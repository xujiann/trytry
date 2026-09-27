"""随访质控「按数量抽」同一批次重跑只补到这个数（P2-640，第十四批「批处理重跑与幂等」扫描 R2-4）。

按比例抽早已按（批次, 随访 id）散列、重跑抽到同一批人（P2-141），按数量抽没跟上：取的是池子里最新的 count 条——两次点击
之间每办结一条随访，最新的几条就换了人，前一次抽的留着、这次又抽进来，count=3 的批次点两次成了 5 条，抽查量越点越多。
修法：已抽的先算进去、只补到 count；已抽的只数同一取数范围（科室、可见机构）里的，别的科室同一天共用默认批次名互不占数。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2640 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2640 患者", "id_card": "330106197010102640", "gender": "女"}).json()["id"]
    return {"org": org, "patient": patient}


def _done(world, dept, n):
    from app.database import SessionLocal
    from app.spd.models import SpdFollowupRecord

    with SessionLocal() as db:
        db.add_all([SpdFollowupRecord(patient_id=world["patient"], org_id=world["org"], dept=dept,
                                      planned_at="2026-09-01", executed_at="2026-09-02", status="done")
                    for _ in range(n)])
        db.commit()


def _plan(client, admin, dept, batch, count):
    resp = client.post(f"{B}/qc-samples/plan", headers=admin, json={"dept": dept, "batch": batch, "count": count})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _sampled(client, admin, batch):
    return client.get(f"{B}/qc-samples", headers=admin, params={"batch": batch, "limit": 500}).json()


def test_同一批次重跑不再越抽越多(client, admin, world):
    _done(world, "P2640 甲科", 6)
    first = _plan(client, admin, "P2640 甲科", "P2640-A", 3)
    assert (first["planned"], first["created"]) == (3, 3)
    _done(world, "P2640 甲科", 2)   # 两次点击之间又办结了两条
    again = _plan(client, admin, "P2640 甲科", "P2640-A", 3)
    assert (again["planned"], again["created"]) == (3, 0), again   # 修前 planned 3、created 2
    assert len(_sampled(client, admin, "P2640-A")) == 3             # 修前 5
    more = _plan(client, admin, "P2640 甲科", "P2640-A", 5)          # 调大数量：只补差的两条
    assert (more["planned"], more["created"]) == (5, 2)


def test_别的科室共用批次名互不占数(client, admin, world):
    _done(world, "P2640 乙科", 4)
    _done(world, "P2640 丙科", 4)
    assert _plan(client, admin, "P2640 乙科", "P2640-B", 2)["created"] == 2
    assert _plan(client, admin, "P2640 丙科", "P2640-B", 2)["created"] == 2
    assert sorted(s["dept"] for s in _sampled(client, admin, "P2640-B")) == ["P2640 丙科"] * 2 + ["P2640 乙科"] * 2
