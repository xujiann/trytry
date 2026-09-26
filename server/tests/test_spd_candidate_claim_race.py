"""目标池认领是先读后写：两个成员同时点认领，两路都读到「没人认领」、都 200，后提交的静默顶掉先认领的（P2-253）。

`POST /api/spd/candidates/{id}/claim` 的 docstring 写着「已被他人认领的返回 409，不静默改人」，判定却是读出
`assigned_user_id` 在内存里比、再赋值提交——flush 出来的 UPDATE 只有 `WHERE id = ?`。与分发（P2-251）同一个窗口。
修法：判定压进 UPDATE（`WHERE assigned_user_id IS NULL OR = 本人`），翻不到即 409。

这里用「认领那一路读完、写库之前，另一位成员先认领并提交」把并发窗口钉成确定的时序。
"""
import pytest

from conftest import login

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2253 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    ids = {}
    for name in ("p2253_first", "p2253_second"):
        created = client.post("/api/users", headers=admin, json={
            "username": name, "password": "pass123456", "role": "doctor", "org_id": org})
        assert created.status_code in (200, 201), created.text
        ids[name] = created.json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2253 患者", "id_card": "330106197609092253"}).json()["id"]
    return {"org": org, "patient": patient, "first": ids["p2253_first"], "second": ids["p2253_second"],
            "second_auth": login(client, "p2253_second", "pass123456")}


def _candidate(world, code):
    from app.database import SessionLocal
    from app.models import SpdCandidate

    with SessionLocal() as db:
        c = SpdCandidate(patient_id=world["patient"], program_code=code, status="suspect", source="screening",
                         org_id=world["org"], risk_level="mid", matched_rules=[], reason="用例夹具")
        db.add(c)
        db.commit()
        return c.id


def test_读完之后别人先认领了_409且不顶掉先认领的(client, world, monkeypatch):
    from app.database import SessionLocal
    from app.models import SpdCandidate
    from app.spd.routers import population

    target = _candidate(world, "P2253_A")
    real_now = population.now_naive
    fired = []

    def claimed_meanwhile():
        if not fired:   # 第二位读完「没人认领」、写库之前：第一位先认领并提交
            fired.append(True)
            with SessionLocal() as other:
                row = other.get(SpdCandidate, target)
                row.assigned_user_id, row.claimed_at = world["first"], real_now()
                other.commit()
        return real_now()

    monkeypatch.setattr(population, "now_naive", claimed_meanwhile)
    resp = client.post(f"{B}/candidates/{target}/claim", headers=world["second_auth"])
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200：先认领的被静默顶掉
    with SessionLocal() as db:
        assert db.get(SpdCandidate, target).assigned_user_id == world["first"]


def test_没人认领的照常认领_疑似转目标_本人重复认领照旧(client, world):
    target = _candidate(world, "P2253_B")
    first = client.post(f"{B}/candidates/{target}/claim", headers=world["second_auth"])
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["assigned_user_id"] == world["second"] and body["status"] == "target" and body["claimed_at"]
    again = client.post(f"{B}/candidates/{target}/claim", headers=world["second_auth"])
    assert again.status_code == 200 and again.json()["assigned_user_id"] == world["second"]
