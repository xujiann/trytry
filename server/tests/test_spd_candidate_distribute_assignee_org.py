"""目标池批量分发的指派人须能以目标患者所属机构的名义办它（P2-725，第十九批「批量 vs 单条」扫描 K1-2）。

分发只查指派人在不在、停没停用（P1-106），不看机构。实测（修前）：甲院医生把甲院的目标患者分给乙院医生 200——乙院医生
按「指给我的」查目标池看不见、认领 403，甲院同事认领 409（已被其他人员认领），这条目标患者谁都办不了，还从中心工作台的
「待分发」里数没了。同一件事在慢专病任务上早已 422（P1-210）。

修法：与任务派人同一判据（`platform.assignee_outside_org`），按分发之后的机构判（带 `org_id` 改挂的按新机构）；整批拒收、
点名是哪几条，不留半成品。已被认领的本来就不动（P2-251），不拦；全域角色照常可分。
"""
import pytest

from app.database import SessionLocal
from app.spd.models import SpdCandidate

B = "/api/spd"


def _login(client, username):
    token = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {k: client.post("/api/organizations", headers=admin, json={
        "name": f"P2725 {k}院", "org_type": "township", "level": "township"}).json()["id"] for k in ("甲", "乙")}
    users = {}
    for key, org, role in (("a1", "甲", "doctor"), ("a2", "甲", "doctor"), ("b1", "乙", "doctor"),
                           ("dir", "乙", "director")):
        r = client.post("/api/users", headers=admin, json={
            "username": f"p2725_{key}", "password": "passw0rd1", "full_name": f"p2725_{key}", "role": role,
            "org_id": orgs[org]})
        assert r.status_code in (200, 201), r.text
        users[key] = r.json()["id"]
    return {"orgs": orgs, "users": users, "n": 0}


def _candidate(client, admin, world, org, claimed_by=None):
    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2725 患者{world['n']}", "id_card": f"33028119800101{world['n']:04d}"}).json()["id"]
    with SessionLocal() as db:
        row = SpdCandidate(patient_id=patient, program_code="hypertension", status="target",
                           org_id=world["orgs"][org], source="screening")
        db.add(row)
        db.commit()
        candidate_id = row.id
    if claimed_by:
        claimed = client.post(f"{B}/candidates/{candidate_id}/claim", headers=_login(client, f"p2725_{claimed_by}"))
        assert claimed.status_code == 200, claimed.text
    return candidate_id


def _distribute(client, headers, world, ids, assignee, **extra):
    return client.post(f"{B}/candidates/distribute", headers=headers, json={
        "candidate_ids": ids, "assigned_user_id": world["users"][assignee], **extra})


def _assigned(candidate_id):
    with SessionLocal() as db:
        return db.get(SpdCandidate, candidate_id).assigned_user_id


def test_分给别家机构的医生_整批422_点名_一条都不动(client, admin, world):
    a1 = _login(client, "p2725_a1")
    first, second = _candidate(client, admin, world, "甲"), _candidate(client, admin, world, "甲")
    resp = _distribute(client, a1, world, [first, second], "b1")
    assert resp.status_code == 422, resp.text   # 修前 200：分过去谁都办不了
    assert str(sorted([first, second])) in resp.json()["detail"], resp.text
    assert _assigned(first) is None and _assigned(second) is None
    # 没分出去，本院同事照常能认领（修前 409「该患者已被其他人员认领」）
    assert client.post(f"{B}/candidates/{first}/claim", headers=_login(client, "p2725_a2")).status_code == 200


def test_分给本院医生或全域角色照常(client, admin, world):
    a1 = _login(client, "p2725_a1")
    mine, center = _candidate(client, admin, world, "甲"), _candidate(client, admin, world, "甲")
    assert _distribute(client, a1, world, [mine], "a2").json() == {"distributed": 1, "not_found": 0}
    assert _distribute(client, a1, world, [center], "dir").status_code == 200   # 全域角色（县级中心）照常
    assert (_assigned(mine), _assigned(center)) == (world["users"]["a2"], world["users"]["dir"])


def test_带org_id改挂的按新机构判(client, admin, world):
    moved = _candidate(client, admin, world, "甲")
    ok = _distribute(client, admin, world, [moved], "b1", org_id=world["orgs"]["乙"])
    assert ok.status_code == 200 and _assigned(moved) == world["users"]["b1"], ok.text
    other = _candidate(client, admin, world, "甲")
    bad = _distribute(client, admin, world, [other], "a1", org_id=world["orgs"]["乙"])
    assert bad.status_code == 422, bad.text   # 改挂到乙院、却指给甲院医生：同样办不了


def test_已被认领的不动_也不拦同批其余的(client, admin, world):
    claimed = _candidate(client, admin, world, "甲", claimed_by="a1")
    fresh = _candidate(client, admin, world, "乙")
    resp = _distribute(client, admin, world, [claimed, fresh], "b1")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"distributed": 1, "not_found": 0, "skipped_claimed": [claimed]}
    assert (_assigned(claimed), _assigned(fresh)) == (world["users"]["a1"], world["users"]["b1"])
