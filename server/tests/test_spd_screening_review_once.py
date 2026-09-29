"""筛查复核只复核一次：已给出定论（确认 / 排除）的再复核 409，「待定」之后还能再复核（P2-736，第十九批「逆操作是否撤净」
扫描 K2-10）。

`review_screening` 原先直接覆写结论与复核人、再按结论翻目标池：A 确认并认领之后，B 在没刷新的页面上点「排除」照 200，
目标池翻成 excluded（认领人仍是 A）、复核人改成 B；再送「待定」，池停在 excluded，筛查记录和目标池两边对不上。前端对已复核
的不再给按钮，接口没挡。修法：判定与写同一条条件 UPDATE（未复核或待定的才写），抢输的 409。
"""
import pytest

from app.database import SessionLocal
from app.models import User
from app.spd.models import SpdCandidate, SpdScreening

B = "/api/spd"
ANSWERS = {"family": "是", "salt": "是", "overweight": "是", "smoke": "否", "drink": "否", "symptom": "是"}


def _login(client, username):
    token = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2736 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    for name in ("a", "b"):
        created = client.post("/api/users", headers=admin, json={
            "username": f"p2736_{name}", "password": "passw0rd1", "full_name": f"P2736 {name}", "role": "doctor",
            "org_id": org})
        assert created.status_code in (200, 201), created.text
    return {"org": org, "a": _login(client, "p2736_a"), "b": _login(client, "p2736_b"), "n": 0}


def _suspect_screening(client, admin, world):
    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2736 患者{world['n']}", "id_card": f"33010219500101{2736 + world['n']:04d}"}).json()["id"]
    client.post("/api/encounters", headers=world["a"], json={"patient_id": patient, "org_id": world["org"],
                                                            "diagnosis_name": "头晕"})
    resp = client.post(f"{B}/screenings", headers=world["a"], json={
        "patient_id": patient, "program_code": "hypertension", "scale_code": "scr_hypertension", "answers": ANSWERS})
    assert resp.status_code == 201 and resp.json()["result"] == "suspect", resp.text
    return resp.json()["id"], patient


def _review(client, headers, screening, result):
    return client.post(f"{B}/screenings/{screening}/review", headers=headers, json={"review_result": result})


def test_确认并认领之后_旧页面上的排除409_目标池不动(client, admin, world):
    screening, patient = _suspect_screening(client, admin, world)
    assert _review(client, world["a"], screening, "confirmed").status_code == 200
    with SessionLocal() as db:
        candidate = db.query(SpdCandidate).filter_by(patient_id=patient, program_code="hypertension").one().id
    assert client.post(f"{B}/candidates/{candidate}/claim", headers=world["a"]).status_code == 200
    stale = _review(client, world["b"], screening, "excluded")
    assert stale.status_code == 409 and "已复核" in stale.json()["detail"], stale.text   # 修前 200
    with SessionLocal() as db:
        assert db.get(SpdCandidate, candidate).status == "target"            # 修前翻成 excluded
        row = db.get(SpdScreening, screening)
        a_id = db.query(User.id).filter(User.username == "p2736_a").scalar()
        assert (row.review_result, row.reviewer_id) == ("confirmed", a_id)   # 修前改成 B 的排除


def test_待定之后还能给出定论(client, admin, world):
    screening, _patient = _suspect_screening(client, admin, world)
    assert _review(client, world["a"], screening, "pending").status_code == 200
    final = _review(client, world["b"], screening, "confirmed")
    assert final.status_code == 200 and final.json()["review_result"] == "confirmed", final.text
