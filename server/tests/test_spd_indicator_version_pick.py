"""同一考核指标并存几版时，计分取哪一版看数据库返回顺序；生效日期一概不看；明细不记用的哪一版（P2-519，第九批
「留痕承诺」扫描 W3-4 的明确部分）。

指标库按（编码, 版本）唯一，建新版本不会停掉旧版本。`run_scoring` 原先不排序地把启用的指标按编码塞进字典，后返回的
那一版覆盖前面的——PG 不保证返回顺序；`effective_from` 建了从不读，下个月才生效的新口径照样拿来算这个月。`run_scoring`
的注释说历史留痕靠 `detail` 里的原始数字，明细却不记版本与公式：同编码几版并存或原地改过口径之后，这期分数是按哪条
公式算出来的无从查起。

修法：同编码取本期已生效的最新一版（按生效日期、再按编号）；全都还没生效的写明「指标在本期尚未生效」；明细追加指标
编号、版本、公式。PATCH 能不能原地改口径（页面写着「各县只需调权重与目标值」）是口径问题，另行登记。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2519 卫生院", "org_type": "township", "level": "township"}).json()["id"]

    def version(tag, formula, effective_from=""):
        resp = client.post(f"{B}/indicators", headers=admin, json={
            "code": "p2519_rate", "name": f"P2519 完成率 {tag}", "data_source": "task", "object_type": "org",
            "formula": formula, "version": tag, "effective_from": effective_from})
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    ids = {"v1": version("v1", "10"), "v2": version("v2", "20", "2026-06-01"), "v3": version("v3", "30", "2031-01-01")}
    plan = client.post(f"{B}/assess-plans", headers=admin, json={
        "code": "p2519_plan", "name": "P2519 方案", "level": "township", "object_type": "org",
        "period_type": "month", "items": [{"indicator_code": "p2519_rate", "weight": 100}]})
    assert plan.status_code == 201, plan.text
    return {"org": org, "plan": plan.json()["id"], "ids": ids}


def _detail(client, admin, world, period):
    run = client.post(f"{B}/scores/run", headers=admin, json={
        "plan_id": world["plan"], "period": period, "object_ids": [world["org"]]})
    assert run.status_code == 200, run.text
    rows = client.get(f"{B}/scores", headers=admin, params={"plan_id": world["plan"], "period": period}).json()
    return client.get(f"{B}/scores/{rows[0]['id']}", headers=admin).json()["detail"][0]


def test_取本期已生效的最新一版_明细记下版本与公式(client, admin, world):
    item = _detail(client, admin, world, "2026-09")
    # 修前：v3（2031 年才生效）照样拿来算、明细里没有版本与公式
    assert (item["value"], item["indicator_id"], item["version"], item["formula"]) == (
        20.0, world["ids"]["v2"], "v2", "20")
    before = _detail(client, admin, world, "2026-03")   # v2 六月才生效，三月按 v1
    assert (before["value"], before["version"]) == (10.0, "v1")


def test_全都还没生效_明说尚未生效(client, admin, world):
    from app.database import SessionLocal
    from app.spd.models import SpdIndicator

    with SessionLocal() as db:   # 只留还没生效的 v3 启用
        db.query(SpdIndicator).filter(SpdIndicator.id.in_([world["ids"]["v1"], world["ids"]["v2"]])).update(
            {SpdIndicator.active: False}, synchronize_session=False)
        db.commit()
    assert _detail(client, admin, world, "2026-09") == {"indicator_code": "p2519_rate", "error": "指标在本期尚未生效"}
