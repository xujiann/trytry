"""随访抽查按比例抽得准、同一批次重跑抽到同一批人（P2-141）。

抽查计划的说明写「抽样按 id 取模而不是随机：同一批次重复调用要抽到同一批人，否则质控员刷新一次页面，
待抽查清单就换了一批」。实现按的却是「这次取到的清单里排第几」：
- 清单按 id 倒序，两次点击之间每办结一条随访，所有位置整体后移——同一批次（默认 QC+当天）重跑就换一批人，
  前一次抽的留着、这次又抽一批，一天点几次，抽查量翻几倍；
- 步长取 `int(1 / 比例)`：页面按 0.05 一档给比例，0.6 抽 100%、0.35 与 0.4 都抽 50%。

修法：按（批次, 随访 id）散列决定抽不抽，与池子里还有谁无关；比例照给的比例。
"""
import pytest

B = "/api/spd"
DEPT = "P2141 质控科"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2141 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2141 患者", "id_card": "330106197010101410", "gender": "男"}).json()["id"]
    return {"org": org, "patient": patient}


def _done_records(world, n):
    from app.database import SessionLocal
    from app.spd.models import SpdFollowupRecord

    with SessionLocal() as db:
        rows = [SpdFollowupRecord(patient_id=world["patient"], org_id=world["org"], dept=DEPT,
                                  planned_at="2026-09-01", executed_at="2026-09-02", status="done")
                for _ in range(n)]
        db.add_all(rows)
        db.commit()
        return [r.id for r in rows]


def _plan(client, admin, **body):
    resp = client.post(f"{B}/qc-samples/plan", headers=admin, json={"dept": DEPT, **body})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _sampled(client, admin, batch):
    rows = client.get(f"{B}/qc-samples", headers=admin, params={"batch": batch, "limit": 500}).json()
    return {r["record_id"] for r in rows}


def test_比例照给的比例抽(client, admin, world):
    _done_records(world, 20)
    plan = _plan(client, admin, ratio=0.6, batch="P2141-A")
    assert plan["pool"] == 20
    assert 11 <= plan["planned"] <= 13, plan   # 修前 20：步长 int(1/0.6) = 1，全抽


def test_同一批次重跑_原有的随访抽不抽不变(client, admin, world):
    first = _plan(client, admin, ratio=0.3, batch="P2141-B")
    before = _sampled(client, admin, "P2141-B")
    assert len(before) == first["planned"] and 5 <= len(before) <= 7
    new_ids = set(_done_records(world, 5))   # 两次点击之间又办结了几条
    _plan(client, admin, ratio=0.3, batch="P2141-B")
    after = _sampled(client, admin, "P2141-B")
    # 修前位置整体后移五格：原有的随访又被抽进一批，批次越抽越多
    assert after - new_ids == before


def test_换一个批次换一批人(client, admin, world):
    _plan(client, admin, ratio=0.3, batch="P2141-C")
    assert _sampled(client, admin, "P2141-C") != _sampled(client, admin, "P2141-B")
