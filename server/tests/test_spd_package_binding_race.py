"""服务包"在绑唯一"下沉到库之后的回归（P1-30）。

洞的形状：`bind_package` 是"先查有没有在绑的、没有就建"——两步之间没有闸门。
PG READ COMMITTED 下并发两路各自查空、各自 INSERT，**不报错、静默写出两条
bound**。危害不是多一行台账：服务包的剩余次数就存在行里（`items[].used`），
两条 bound 意味着配额凭空翻倍，扣减还会分裂在两条上；居民端与个案管理师
工作台都按 `status='bound'` 逐条渲染，会出现两张同名服务包卡片。

`uq_spd_pkg_binding_enroll_pkg_bound` 把这条不变式下沉为**部分唯一索引**
（只锁 `status='bound'`），`_bind_package` 把 flush 时的 IntegrityError 翻成
与预检**同一句** 409。写成全量唯一是另一种坏：解绑保留台账不删记录、
解绑后重绑是合法续期路径，全量唯一会把它们一并拒掉。

本档钉四件事：

1. **行为面**：顺序重复 → 409 且文案一致；解绑后重绑 → 201（合法多条不受影响）。
2. **兜底本身**：绕开路由预检直接连着调 `_bind_package`——那正是并发抢输者
   实际到达的位置——必须拿到 409「该服务包已绑定」而不是 500 或 IntegrityError。
3. **防拆卸**：索引必须留在模型上，带 `unique=True` 与 `status = 'bound'` 部分条件，
   并且真的建在库上。SQLite 的库级写锁让线程探针对"拆掉索引"不敏感（拆了照样
   大概率不重复），静态钉与直插证明才是确定性的网——与
   `test_logical_unique_races.py` 同一分工。
4. **真 PG 竞态**：8 路只成一条，见 `test_spd_package_binding_unique_races.py`。
"""
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect as sa_inspect

from conftest import reset_database

from app.database import engine
from app.main import app
from app.models import Base

B = "/api/spd"
DUP_DETAIL = "该服务包已绑定"


@pytest.fixture(scope="module")
def client():
    reset_database()
    # raise_server_exceptions=False：兜底若失灵，这里要看到 500 而不是异常穿透测试进程
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def h(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def world(client, h):
    """机构 + 病种 + 服务包；纳管档案各用例现造（一条档案一条不变式，互不串味）。"""
    org = client.post(
        "/api/organizations",
        json={"name": "服务包竞态卫生院", "org_type": "township", "level": "township"},
        headers=h,
    ).json()
    program = client.post(
        f"{B}/programs",
        json={"code": "pkgrace_dm", "name": "竞态糖尿病", "category": "chronic",
              "stages": [{"key": "s1", "name": "一期"}]},
        headers=h,
    ).json()
    package = client.post(
        f"{B}/service-packages",
        json={"code": "pkgrace_pkg", "name": "竞态服务包", "program_code": "pkgrace_dm",
              "price": 200, "period_days": 30,
              "items": [{"code": "bp_check", "name": "血压测量", "times": 2, "price": 5}]},
        headers=h,
    ).json()
    package2 = client.post(
        f"{B}/service-packages",
        json={"code": "pkgrace_pkg2", "name": "竞态服务包二", "program_code": "pkgrace_dm",
              "price": 100, "period_days": 30,
              "items": [{"code": "edu", "name": "健康宣教", "times": 1, "price": 10}]},
        headers=h,
    ).json()
    return {"org": org, "program": program, "package": package, "package2": package2,
            "seq": iter(range(1, 99))}


def _enrollment(client, h, world, name: str, id_card: str) -> int:
    """现造一名患者与其纳管档案，返回 enrollment_id。"""
    patient = client.post(
        "/api/patients",
        json={"name": name, "id_card": id_card, "gender": "男", "birth_date": "1960-01-01"},
        headers=h,
    ).json()
    resp = client.post(
        f"{B}/enrollments",
        json={"patient_id": patient["id"], "program_code": "pkgrace_dm",
              "org_id": world["org"]["id"]},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _bind(client, h, eid: int, package_id: int):
    return client.post(f"{B}/enrollments/{eid}/packages",
                       json={"package_id": package_id}, headers=h)


# ================================================================ 行为面


def test_顺序重复绑同一服务包给出同一句409(client, h, world):
    """预检这条快路径的文案就是并发抢输者要拿到的文案——两处必须一模一样。"""
    eid = _enrollment(client, h, world, "服务包竞态一", "330881196001011001")
    first = _bind(client, h, eid, world["package"]["id"])
    assert first.status_code == 201, first.text

    again = _bind(client, h, eid, world["package"]["id"])
    assert again.status_code == 409, again.text
    assert again.json() == {"detail": DUP_DETAIL}

    # 只锁 (enrollment, package) 这一对：同一档案换个服务包照绑不误
    other = _bind(client, h, eid, world["package2"]["id"])
    assert other.status_code == 201, other.text


def test_解绑后重绑仍是201_部分索引不能写成全量唯一(client, h, world):
    """解绑保留台账不删记录、解绑后重绑是合法续期——全量唯一会把这条路堵死。

    档案详情按 enrollment 列出**全部**绑定（含历史），所以这里应看到两条：
    一条 unbound 的旧台账 + 一条新的 bound。
    """
    eid = _enrollment(client, h, world, "服务包竞态二", "330881196001011002")
    first = _bind(client, h, eid, world["package"]["id"])
    assert first.status_code == 201, first.text
    binding_id = first.json()["id"]

    unbound = client.post(f"{B}/package-bindings/{binding_id}/unbind", headers=h)
    assert unbound.status_code == 200, unbound.text
    assert unbound.json()["status"] == "unbound"

    rebound = _bind(client, h, eid, world["package"]["id"])
    assert rebound.status_code == 201, rebound.text
    assert rebound.json()["id"] != binding_id

    packages = client.get(f"{B}/enrollments/{eid}", headers=h).json()["packages"]
    assert [p["status"] for p in packages] == ["unbound", "bound"]

    # 续期后的这条重复绑定，照样被同一句 409 挡住
    again = _bind(client, h, eid, world["package"]["id"])
    assert again.status_code == 409
    assert again.json() == {"detail": DUP_DETAIL}


def test_兜底路径直接给出409而不是500(client, h, world):
    """绕开路由预检，连着调两次 `_bind_package`——并发抢输者到达的正是这里。

    预检在顺序请求下就会给出 409，行为用例因此**分辨不出**兜底是否真的接上；
    这里直接把兜底那段代码打在明处：撞索引必须翻成 409「该服务包已绑定」，
    而不是把 IntegrityError 抛成 500，也不能因为漏了 rollback 让会话烂在
    aborted 状态（PG 上后续审计写入会连带失败）。
    """
    from fastapi import HTTPException

    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment
    from app.spd.routers.population import _bind_package

    eid = _enrollment(client, h, world, "服务包竞态三", "330881196001011003")
    pkg_id = world["package"]["id"]

    db = SessionLocal()
    try:
        enrollment = db.get(SpdEnrollment, eid)
        _bind_package(db, enrollment, pkg_id)
        db.commit()

        enrollment = db.get(SpdEnrollment, eid)
        with pytest.raises(HTTPException) as excinfo:
            _bind_package(db, enrollment, pkg_id)
        assert excinfo.value.status_code == 409
        assert excinfo.value.detail == DUP_DETAIL
        # rollback 已在兜底里做过：会话还能继续用（PG 上这正是 409 与 500 的分界）
        assert db.get(SpdEnrollment, eid) is not None
    finally:
        db.close()


def test_八路并发绑定只成一条其余同一句409(client, h, world):
    """并发探针：SQLite 的库级写锁让它对"拆掉索引"不敏感，所以它证的不是索引，
    而是**并发下不会 500、不会写出两条**；索引在不在由下面的静态钉与直插用例守。

    用 Barrier 卡住 8 个线程一起放行：只靠"起 8 个线程"竞态窗口根本打不开
    （线程创建本身有先后，前一个常常已经提交完了后一个才开始读）。
    """
    eid = _enrollment(client, h, world, "服务包竞态四", "330881196001011004")
    pkg_id = world["package"]["id"]

    codes: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def run():
        barrier.wait(timeout=30)
        code = _bind(client, h, eid, pkg_id).status_code
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(codes) == [201] + [409] * 7, f"实际状态码：{sorted(codes)}"

    bound = [p for p in client.get(f"{B}/enrollments/{eid}", headers=h).json()["packages"]
             if p["status"] == "bound"]
    assert len(bound) == 1, f"应只剩一条在绑，实际 {len(bound)} 条（配额被翻倍）"


# ================================================================ 防拆卸静态钉


def test_在绑唯一的部分索引不许消失():
    """模型侧的声明就是这条不变式的落点，删掉就等于把静默双写的洞放回去。

    同时钉住"是部分索引"：条件写没了就变成全量唯一，解绑后重绑会被拒——
    那是另一种坏，而且是业务天天走的路。
    """
    index = next(
        (i for i in Base.metadata.tables["spd_package_bindings"].indexes
         if i.name == "uq_spd_pkg_binding_enroll_pkg_bound"),
        None,
    )
    assert index is not None, "uq_spd_pkg_binding_enroll_pkg_bound 没了——静默双写的洞回来了"
    assert index.unique, "该索引不再是唯一索引，等于没有约束"
    assert [c.name for c in index.columns] == ["enrollment_id", "package_id"], "索引的键变了"
    for dialect in ("sqlite", "postgresql"):
        where = str(index.dialect_options[dialect].get("where", ""))
        assert "status = 'bound'" in where, (
            f"{dialect} 侧的部分条件不再是 status = 'bound'："
            "全量唯一会拒掉解绑后重绑这条合法续期路径"
        )


def test_在绑唯一索引真的建在库上():
    """模型声明了、库里没建过（漏迁移）同样等于没有约束——按真实表结构再钉一遍。"""
    names = {i["name"] for i in sa_inspect(engine).get_indexes("spd_package_bindings")}
    assert "uq_spd_pkg_binding_enroll_pkg_bound" in names, (
        "spd_package_bindings 上没有 uq_spd_pkg_binding_enroll_pkg_bound（库与模型对不上）"
    )


def test_绕开接口层直插时库里真的拦得住(client, h, world):
    """索引"在不在"与"拦不拦得住"是两回事：直接写库，看数据库自己是否抬手。

    顺带钉住部分条件的另一半——两条 unbound 的历史台账必须能共存，
    否则解绑保留台账这条既有行为就被约束反噬了。
    """
    from sqlalchemy.exc import IntegrityError

    from app.database import SessionLocal
    from app.spd.models import SpdPackageBinding

    eid = _enrollment(client, h, world, "服务包竞态五", "330881196001011005")
    pkg_id = world["package"]["id"]
    assert _bind(client, h, eid, pkg_id).status_code == 201

    db = SessionLocal()
    try:
        db.add(SpdPackageBinding(enrollment_id=eid, package_id=pkg_id, items=[],
                                 status="bound", period_end=""))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        for _ in range(2):
            db.add(SpdPackageBinding(enrollment_id=eid, package_id=pkg_id, items=[],
                                     status="unbound", period_end=""))
            db.commit()
    finally:
        db.close()
