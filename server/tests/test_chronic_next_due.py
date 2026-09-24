"""慢病「下次随访日」走日期真源（P2-55）。

建档（`ChronicCreate.next_due`）与随访（`FollowUpCreate.next_due`）原先是裸 `str`：日期闸门按字段名里的
`date` 认日期字段，`next_due` 一直不在视野里；两端随访表单上这一格又是自由文本框。2026-09-24 开发库实测
（修前代码）：建档填「2026/10/1」201 照存，随访填「10月1日」201 照存。超期名单按字符串比较——
「10月1日」排在一切「2026-…」之前，还没到期就进了超期名单、而且永远出不去；「2026/10/1」的「/」排在
「-」之后，到了 2026-12-01 已超期两个月却不在名单里。

修法：两个字段换 `OptionalDateStr`（留空照旧按病种周期自动建议），出参覆盖回 `str`（P1-63：换真源之前
存进去的坏值要原样读出来，而不是让清单 500）；两端表单换日期控件。
"""
import pytest

from app.database import SessionLocal
from app.models import ChronicPatient, FollowUp

BAD = ["2026/10/1", "10月1日", "2026-02-31"]


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations",
                      json={"name": "随访日期卫生院", "org_type": "township", "level": "township"},
                      headers=admin).json()
    patients = [
        client.post("/api/patients",
                    json={"name": f"随访日期患者{i}", "id_card": f"33028119900101{i:04d}", "gender": "男"},
                    headers=admin).json()
        for i in range(3)
    ]
    return {"org": org, "patients": patients}


def _next_due_errors(resp):
    assert resp.status_code == 422, (resp.status_code, resp.text[:200])
    return [e for e in resp.json()["detail"] if e.get("loc") == ["body", "next_due"]]


def test_建档填非日期的下次随访日_422_不建档(client, admin, world):
    patient_id = world["patients"][0]["id"]
    for bad in BAD:
        resp = client.post("/api/chronic",
                           json={"patient_id": patient_id, "disease": "hypertension",
                                 "managed_by_org_id": world["org"]["id"], "next_due": bad},
                           headers=admin)
        assert _next_due_errors(resp), bad
    db = SessionLocal()
    try:
        assert db.query(ChronicPatient).filter(ChronicPatient.patient_id == patient_id).count() == 0
    finally:
        db.close()


def test_随访填非日期的下次随访日_422_档案的下次随访日不被改掉(client, admin, world):
    chronic = client.post("/api/chronic",
                          json={"patient_id": world["patients"][1]["id"], "disease": "hypertension",
                                "managed_by_org_id": world["org"]["id"], "next_due": "2026-10-01"},
                          headers=admin)
    assert chronic.status_code == 201, chronic.text
    cid = chronic.json()["id"]
    for bad in BAD:
        assert _next_due_errors(client.post(f"/api/chronic/{cid}/followups",
                                            json={"sbp": 130, "dbp": 80, "next_due": bad}, headers=admin)), bad
    db = SessionLocal()
    try:
        assert db.get(ChronicPatient, cid).next_due == "2026-10-01"
        assert db.query(FollowUp).filter(FollowUp.chronic_id == cid).count() == 0
    finally:
        db.close()

    # 合法日期照收；留空照旧按病种周期自动建议
    manual = client.post(f"/api/chronic/{cid}/followups",
                         json={"sbp": 130, "dbp": 80, "next_due": "2026-11-01"}, headers=admin)
    assert manual.status_code == 201, manual.text
    assert manual.json()["next_due"] == "2026-11-01" and manual.json()["next_due_suggested"] is False
    auto = client.post(f"/api/chronic/{cid}/followups", json={"sbp": 130, "dbp": 80, "next_due": ""},
                       headers=admin)
    assert auto.status_code == 201, auto.text
    assert auto.json()["next_due_suggested"] is True and auto.json()["next_due"]


def test_存量非日期的下次随访日照常读出_不拖垮清单(client, admin, world):
    """换真源之前存进去的坏值：档案清单、超期名单、随访记录都照常 200，坏值原样读出。"""
    db = SessionLocal()
    try:
        row = ChronicPatient(patient_id=world["patients"][2]["id"], disease="diabetes",
                             managed_by_org_id=world["org"]["id"], level=1, next_due="2026/10/1")
        db.add(row)
        db.flush()
        db.add(FollowUp(chronic_id=row.id, next_due="10月1日"))
        db.commit()
        cid = row.id
    finally:
        db.close()

    listed = client.get("/api/chronic", params={"disease": "diabetes"}, headers=admin)
    assert listed.status_code == 200, listed.text
    assert {c["id"]: c["next_due"] for c in listed.json()}[cid] == "2026/10/1"
    assert client.get("/api/chronic/overdue", params={"today": "2027-01-01"}, headers=admin).status_code == 200
    followups = client.get(f"/api/chronic/{cid}/followups", headers=admin)
    assert followups.status_code == 200, followups.text
    assert [f["next_due"] for f in followups.json()] == ["10月1日"]
