"""考核方案里同一指标写两次按两次计分（P2-1276，第三十七批「一次请求、一次导入里的重复元素」扫描 AA2-5）。

`spd/routers/assess.py::_check_plan_items` 建 / 改方案只查指标在不在、不查重复；`run_scoring` 逐条累加；指标「被引用」页
（`indicator_usage`）取 `next(...)` 第一条的权重。修前实测：`enroll_rate:40, followup_rate:60, enroll_rate:40` 下甲院 80 分
（同样的数据不重复时 40 分），分项明细两条 enroll_rate，引用页说这个方案权重 40。服务包项目编码重复 422（P2-631）、随访时间点
重复 422（P2-719）、量表题目 key 重复 422，考核方案的指标是同一种重复却没拦。

修法：建 / 改方案时指标编码重复 422 并点名；存量里重复的方案计分时同一指标只计首条（与引用页取第一条同一个口径），分项明细
只出一条。不重复的方案分数不变。
"""
import pytest
from conftest import business_today

B = "/api/spd"
OK_ITEMS = [{"indicator_code": "enroll_rate", "weight": 40}, {"indicator_code": "followup_rate", "weight": 60}]
DUP_DETAIL = "考核方案里指标重复：enroll_rate（同一指标只写一条，写两条会按两次计分）"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21276 甲卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21276 患者", "id_card": "330106196001011276", "gender": "男", "birth_date": "1960-01-01"})
    assert patient.status_code in (200, 201), patient.text
    enrolled = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient.json()["id"], "program_code": "hypertension", "org_id": org})
    assert enrolled.status_code == 201, enrolled.text
    return {"org": org, "period": business_today().strftime("%Y-%m")}


def _plan(client, admin, code, items):
    return client.post(f"{B}/assess-plans", headers=admin, json={
        "code": code, "name": f"{code} 方案", "level": "township", "object_type": "org", "period_type": "month",
        "items": items})


def _run(client, admin, world, plan_id):
    """跑一次分，返回甲院的总分与分项明细（指标、权重、得分）。"""
    run = client.post(f"{B}/scores/run", headers=admin,
                      json={"plan_id": plan_id, "period": world["period"], "object_ids": [world["org"]]})
    assert run.status_code == 200, run.text[:300]
    rows = client.get(f"{B}/scores?plan_id={plan_id}&period={world['period']}", headers=admin).json()
    detail = client.get(f"{B}/scores/{rows[0]['id']}", headers=admin).json()
    return detail["total_score"], [(d["indicator_code"], d.get("weight"), d.get("score")) for d in detail["detail"]]


def test_建方案_同一指标写两条_422点名(client, admin, world):
    resp = _plan(client, admin, "P21276_NEW", [*OK_ITEMS, {"indicator_code": "enroll_rate", "weight": 40}])
    assert (resp.status_code, resp.json()) == (422, {"detail": DUP_DETAIL}), resp.text   # 修前 201
    both = _plan(client, admin, "P21276_NEW2", [*OK_ITEMS, *OK_ITEMS])
    assert both.status_code == 422, both.text
    assert both.json()["detail"].startswith("考核方案里指标重复：enroll_rate、followup_rate（")


def test_改方案_同一指标写两条_422(client, admin, world):
    created = _plan(client, admin, "P21276_EDIT", OK_ITEMS)
    assert created.status_code == 201, created.text
    resp = client.patch(f"{B}/assess-plans/{created.json()['id']}", headers=admin, json={
        "items": [*OK_ITEMS, {"indicator_code": "enroll_rate", "weight": 10}]})
    assert (resp.status_code, resp.json()) == (422, {"detail": DUP_DETAIL}), resp.text   # 修前 200
    assert client.patch(f"{B}/assess-plans/{created.json()['id']}", headers=admin,
                        json={"items": OK_ITEMS[::-1]}).status_code == 200   # 不重复的照常改


def test_不重复的方案分数照旧(client, admin, world):
    created = _plan(client, admin, "P21276_OK", OK_ITEMS)
    assert created.status_code == 201, created.text
    assert _run(client, admin, world, created.json()["id"]) == (
        40.0, [("enroll_rate", 40.0, 40.0), ("followup_rate", 60.0, 0.0)])   # 与修前同一个分


def test_存量重复方案_同一指标只计首条_明细一条_与引用页同口径(client, admin, world):
    from app.database import SessionLocal
    from app.spd.models import SpdAssessPlan

    created = _plan(client, admin, "P21276_LEGACY", OK_ITEMS)
    assert created.status_code == 201, created.text
    plan_id = created.json()["id"]
    with SessionLocal() as db:   # 修前存下的：同一指标末尾又追加了一条（权重写成 30，看得出取的是哪一条）
        db.get(SpdAssessPlan, plan_id).items = [*OK_ITEMS, {"indicator_code": "enroll_rate", "weight": 30}]
        db.commit()
    assert _run(client, admin, world, plan_id) == (
        40.0, [("enroll_rate", 40.0, 40.0), ("followup_rate", 60.0, 0.0)])   # 修前 70 分、明细两条 enroll_rate
    indicators = client.get(f"{B}/indicators?limit=100", headers=admin).json()
    enroll = next(i for i in indicators if i["code"] == "enroll_rate")
    usage = client.get(f"{B}/indicators/{enroll['id']}/usage", headers=admin).json()
    assert [p["weight"] for p in usage["plans"] if p["id"] == plan_id] == [40]   # 引用页同样报首条的权重
