"""目标池分发把已被认领的患者静默改给别人：分发页写着「已被认领的患者不会被覆盖」（P2-251）。

`POST /api/spd/candidates/distribute` 原先对整批一律改团队、责任人、机构：团队成员刚认领（`claim` 置了责任人与
认领时间）的患者被分发改给了另一个人，认领时间还留着，回执照样算「已分发」。分发页的说明写着「按辖区、病种、风险
把目标人群分给服务团队；已被认领的患者不会被覆盖」。

修法：已被认领的（`claimed_at` 有值）不动，判定压进 UPDATE——载入整批之后才被认领的同样不覆盖；回执加
`skipped_claimed` 列出没动的编号，页面把分了几条、哪些已被认领说出来。
"""
import pytest

from conftest import login

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdTeam

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2251 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    ids = {}
    for name in ("p2251_member", "p2251_other"):
        created = client.post("/api/users", headers=admin, json={
            "username": name, "password": "pass123456", "role": "doctor", "org_id": org})
        assert created.status_code in (200, 201), created.text
        ids[name] = created.json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2251 患者", "id_card": "330106197508082251"}).json()["id"]
    with SessionLocal() as db:
        team = SpdTeam(name="P2251 团队", org_id=org, level="township", active=True)
        db.add(team)
        db.commit()
        team_id = team.id
    return {"org": org, "patient": patient, "team": team_id, "member": ids["p2251_member"],
            "other": ids["p2251_other"], "member_auth": login(client, "p2251_member", "pass123456")}


_seq = iter(range(1, 100))


def _candidate(world, status="target"):
    """一位患者一个病种只有一条目标池记录（唯一键），每条换一个病种编码。"""
    from app.database import SessionLocal
    from app.models import SpdCandidate

    with SessionLocal() as db:
        c = SpdCandidate(patient_id=world["patient"], program_code=f"P2251_PG{next(_seq)}", status=status,
                         source="screening",
                         org_id=world["org"], risk_level="mid", matched_rules=[], reason="用例夹具")
        db.add(c)
        db.commit()
        return c.id


def _row(candidate_id):
    from app.database import SessionLocal
    from app.models import SpdCandidate

    with SessionLocal() as db:
        c = db.get(SpdCandidate, candidate_id)
        return {"team_id": c.team_id, "assigned_user_id": c.assigned_user_id, "status": c.status,
                "claimed": c.claimed_at is not None}


def test_已被认领的不覆盖_没认领的照分_回执说清(client, admin, world):
    claimed, free, suspect = _candidate(world), _candidate(world), _candidate(world, status="suspect")
    assert client.post(f"{B}/candidates/{claimed}/claim", headers=world["member_auth"]).status_code == 200
    resp = client.post(f"{B}/candidates/distribute", headers=admin, json={
        "candidate_ids": [claimed, free, suspect, 987654321], "team_id": world["team"],
        "assigned_user_id": world["other"]})
    assert resp.status_code == 200, resp.text
    # 修前 {"distributed": 3, "not_found": 1}：认领了的那条也算分发、被改给了别人
    assert resp.json() == {"distributed": 2, "not_found": 1, "skipped_claimed": [claimed]}
    assert _row(claimed) == {"team_id": None, "assigned_user_id": world["member"], "status": "target", "claimed": True}
    assert _row(free) == {"team_id": world["team"], "assigned_user_id": world["other"], "status": "target",
                          "claimed": False}
    assert _row(suspect)["status"] == "target"   # 疑似照旧转目标


def test_载入整批之后才被认领的同样不覆盖(client, admin, world, monkeypatch):
    from app.database import SessionLocal
    from app.models import SpdCandidate
    from app.models._base import utcnow
    from app.spd.routers import population

    target = _candidate(world)
    real_guard = population.assert_org_writable
    fired = []

    def claimed_meanwhile(db, user, org_id):
        if not fired:   # 分发那一路载入整批之后、写库之前：团队成员先认领并提交
            fired.append(True)
            with SessionLocal() as other:
                row = other.get(SpdCandidate, target)
                row.assigned_user_id, row.claimed_at = world["member"], utcnow()
                other.commit()
        return real_guard(db, user, org_id)

    monkeypatch.setattr(population, "assert_org_writable", claimed_meanwhile)
    resp = client.post(f"{B}/candidates/distribute", headers=admin, json={
        "candidate_ids": [target], "team_id": world["team"], "assigned_user_id": world["other"]})
    assert fired and resp.status_code == 200, resp.text
    assert resp.json()["skipped_claimed"] == [target]
    assert _row(target)["assigned_user_id"] == world["member"]   # 修前被改给了 other


def test_没有已认领的_回执逐字节照旧(client, admin, world):
    free = _candidate(world)
    resp = client.post(f"{B}/candidates/distribute", headers=admin, json={
        "candidate_ids": [free], "team_id": world["team"]})
    assert resp.status_code == 200 and resp.json() == {"distributed": 1, "not_found": 0}


def test_页面回执说出已被认领没动的():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")
    start = source.index('$("#spd-dist-form").onsubmit')
    body = source[start: source.index('$("#spd-crt-form").onsubmit', start)]
    assert "skipped_claimed" in body and "已被认领、未改动" in body
