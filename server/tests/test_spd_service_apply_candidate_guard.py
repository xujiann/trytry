"""受理居民服务申请时改派目标池候选：与「认领」同一口径（P0-30）。

`POST /api/spd/service-applies/{id}/handle` 受理即把居民放进目标池。目标池里**已有**这位居民时
（别家筛出来、别家已认领的），原先直接把那条候选改派给调用方——2026-09-24 取证代理实测：
乙院受理一条居民申请，就把甲院认领的人挪进了自己名下（200）；而同样的事走「认领」
（`claim_candidate`）是 403，那里早就 `assert_org_writable(candidate.org_id)`。

照认领的口径补上。**只修这一个分支**：目标池里没有这位居民时，谁来受理、放进哪家的池子，
是 P1-48 待裁定的口径（申请表没有机构列），这里不动——用例里也钉住"照旧"。
"""
import itertools

import pytest

from app.database import SessionLocal
from app.spd.models import SpdCandidate, SpdServiceApply


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def apply_world(client):
    admin = _login(client, "admin", "admin123")
    orgs, users = {}, {}
    for key, name in (("a", "申请受理甲院"), ("x", "申请受理乙院")):
        orgs[key] = client.post("/api/organizations",
                                json={"name": name, "org_type": "township", "level": "township"},
                                headers=admin).json()["id"]
        r = client.post("/api/users",
                        json={"username": f"p030_doc_{key}", "password": "pw123456", "full_name": f"{name}医生",
                              "role": "doctor", "org_id": orgs[key]},
                        headers=admin)
        assert r.status_code == 201, r.text
        users[key] = r.json()["id"]
    seq = itertools.count(1)

    def new_patient() -> int:
        n = next(seq)
        return client.post("/api/patients",
                           json={"name": f"申请受理患者{n}", "id_card": f"3200001988060666{70 + n}"},
                           headers=admin).json()["id"]

    def add(row) -> int:
        db = SessionLocal()
        try:
            db.add(row)
            db.commit()
            return row.id
        finally:
            db.close()

    return {"orgs": orgs, "users": users, "doc_a": _login(client, "p030_doc_a"),
            "doc_x": _login(client, "p030_doc_x"), "new_patient": new_patient, "add": add}


def _candidate(candidate_id: int) -> tuple[str, int | None, int | None]:
    db = SessionLocal()
    try:
        c = db.get(SpdCandidate, candidate_id)
        return c.status, c.org_id, c.assigned_user_id
    finally:
        db.close()


def _apply_status(apply_id: int) -> str:
    db = SessionLocal()
    try:
        return db.get(SpdServiceApply, apply_id).status
    finally:
        db.close()


def _world_with_candidate(w):
    pid = w["new_patient"]()
    cid = w["add"](SpdCandidate(patient_id=pid, program_code="hypertension", status="suspect",
                                org_id=w["orgs"]["a"], assigned_user_id=w["users"]["a"], reason="甲院筛出"))
    aid = w["add"](SpdServiceApply(patient_id=pid, program_code="hypertension", note="想加入高血压管理"))
    return cid, aid


def test_别家不能借受理申请改派本院的候选(client, apply_world):
    cid, aid = _world_with_candidate(apply_world)
    r = client.post(f"/api/spd/service-applies/{aid}/handle", json={"status": "accepted"},
                    headers=apply_world["doc_x"])
    assert r.status_code == 403, r.text
    assert _candidate(cid) == ("suspect", apply_world["orgs"]["a"], apply_world["users"]["a"])
    assert _apply_status(aid) == "pending", "被拒的受理不能把申请标成已处理"


def test_候选所属机构照常受理(client, apply_world):
    cid, aid = _world_with_candidate(apply_world)
    r = client.post(f"/api/spd/service-applies/{aid}/handle", json={"status": "accepted"},
                    headers=apply_world["doc_a"])
    assert r.status_code == 200, r.text
    assert _candidate(cid) == ("target", apply_world["orgs"]["a"], apply_world["users"]["a"])
    assert _apply_status(aid) == "accepted"


def test_目标池里没有这位居民时照旧放进受理方的池子(client, apply_world):
    """这一支的口径（谁能受理、放进哪家）属 P1-48 待裁定，本修复不改——钉住"照旧"。"""
    pid = apply_world["new_patient"]()
    aid = apply_world["add"](SpdServiceApply(patient_id=pid, program_code="hypertension"))
    r = client.post(f"/api/spd/service-applies/{aid}/handle", json={"status": "accepted"},
                    headers=apply_world["doc_x"])
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        (c,) = db.query(SpdCandidate).filter(SpdCandidate.patient_id == pid).all()
        assert (c.status, c.org_id, c.assigned_user_id) == (
            "target", apply_world["orgs"]["x"], apply_world["users"]["x"])
    finally:
        db.close()
