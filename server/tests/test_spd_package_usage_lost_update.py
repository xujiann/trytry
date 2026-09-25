"""慢专病服务包扣减登记是 JSON 读改写：两笔并发扣减各读到同一个已用次数，后写的盖掉先写的，服务包被用超（P2-112）。

`add_usage` 原先「读 binding.items → 判剩余 → 已用 +qty → 整列写回 → 记一条扣减流水」，没有任何锁。次数记在 JSON 列里，
两种方言都没有可移植的「原子改数组里某一项」，PG 的 READ COMMITTED 下两笔并发扣减都读到同一个已用次数：

- 两笔各记了一条扣减流水，已用次数只加了一次——账上还剩的次数比实际多，服务包被用超；
- 剩最后一次时两笔同时扣：两笔都判定「还剩一次」、都成功——一次的项目用了两次；
- 解绑与扣减同时到：扣减读到的还是「在绑」，一笔扣减记在了已解绑的服务包上。

修法同召回联系记录（P1-47 一族）：`concurrency.serialized_on` 锁住这条绑定、重读、再判再写，状态也在锁里重判。

这里用「一路拿着先读到的对象、另一路先提交」把并发窗口钉成确定的时序（SQLite 与 PG 同样成立）；
PG 上真并发的不变量见 test_spd_package_usage_races.py。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2112 服务包院", "org_type": "township", "level": "township"}).json()["id"]
    r = client.post(f"{B}/programs", headers=admin, json={"code": "P2112_PG", "name": "P2112 病种", "category": "chronic"})
    assert r.status_code == 201, r.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2112 患者", "id_card": "330106197002021123", "gender": "男", "birth_date": "1970-02-02"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin,
                             json={"patient_id": patient, "program_code": "P2112_PG", "org_id": org})
    assert enrollment.status_code == 201, enrollment.text
    package = client.post(f"{B}/service-packages", headers=admin, json={
        "code": "p2112_pkg", "name": "P2112 服务包", "program_code": "P2112_PG", "price": 100, "period_days": 30,
        "items": [{"code": "bp_check", "name": "血压测量", "times": 2, "price": 5},
                  {"code": "edu", "name": "健康宣教", "times": 1, "price": 10},
                  {"code": "visit", "name": "上门随访", "times": 3, "price": 20}]})
    assert package.status_code == 201, package.text
    bound = client.post(f"{B}/enrollments/{enrollment.json()['id']}/packages", headers=admin,
                        json={"package_id": package.json()["id"]})
    assert bound.status_code == 201, bound.text
    return {"binding": bound.json()["id"]}


def _usage(db, binding_id, item_code):
    from app.models import User
    from app.spd.routers.population import UsageIn, add_usage

    return add_usage(binding_id, UsageIn(item_code=item_code), db=db,
                     user=db.query(User).filter_by(username="admin").one())


def _ledger(binding_id, item_code):
    """(JSON 里记的已用次数, 扣减流水里这一项的次数合计)——两者必须相等。"""
    from app.database import SessionLocal
    from app.spd.models import SpdPackageBinding, SpdPackageUsage

    with SessionLocal() as db:
        item = next(i for i in db.get(SpdPackageBinding, binding_id).items if i["code"] == item_code)
        logged = sum(u.qty for u in db.query(SpdPackageUsage).filter_by(binding_id=binding_id, item_code=item_code))
        return item["used"], logged


def test_两笔并发扣减_已用次数与扣减流水对得上(world):
    from app.database import SessionLocal
    from app.spd.models import SpdPackageBinding

    binding = world["binding"]
    with SessionLocal() as racer, SessionLocal() as winner:
        held = racer.get(SpdPackageBinding, binding)   # noqa: F841 — 第一笔先读到已用 0；留着引用，身份映射是弱引用
        _usage(winner, binding, "bp_check")          # 第二笔先扣成功：已用 1
        _usage(racer, binding, "bp_check")           # 第一笔拿着读到的已用 0 接着扣
    assert _ledger(binding, "bp_check") == (2, 2)   # 修前 (1, 2)：两条流水、次数只加了一次


def test_剩最后一次时两笔同时扣_只成一笔(world):
    from fastapi import HTTPException

    from app.database import SessionLocal
    from app.spd.models import SpdPackageBinding

    binding = world["binding"]
    with SessionLocal() as racer, SessionLocal() as winner:
        held = racer.get(SpdPackageBinding, binding)   # noqa: F841 — 第一笔先读到「还剩一次」
        _usage(winner, binding, "edu")               # 第二笔先把最后一次用掉
        with pytest.raises(HTTPException) as exc:
            _usage(racer, binding, "edu")
    assert exc.value.status_code == 409 and exc.value.detail == "该项目剩余次数不足"   # 修前不报错
    assert _ledger(binding, "edu") == (1, 1)   # 修前 (1, 2)：一次的项目用了两次


def test_解绑与扣减同时到_不在已解绑的服务包上扣(world):
    from fastapi import HTTPException

    from app.database import SessionLocal
    from app.models import User
    from app.spd.models import SpdPackageBinding
    from app.spd.routers.population import unbind_package

    binding = world["binding"]
    with SessionLocal() as racer, SessionLocal() as winner:
        held = racer.get(SpdPackageBinding, binding)   # noqa: F841 — 扣减那一路先读到「在绑」
        unbind_package(binding, db=winner, user=winner.query(User).filter_by(username="admin").one())
        with pytest.raises(HTTPException) as exc:
            _usage(racer, binding, "visit")
    assert exc.value.status_code == 409 and exc.value.detail == "该服务包已解绑，不能扣减"   # 修前不报错
    assert _ledger(binding, "visit") == (0, 0)   # 修前 (1, 1)：扣减记在了已解绑的服务包上
