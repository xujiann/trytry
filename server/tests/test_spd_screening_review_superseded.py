"""复核一条已过时的筛查不得再翻目标池行（P2-1572，第四十六批扫描 AJ3-1）。

同一位居民复筛一次、或批量识别重跑一次，就有两条未复核的疑似（P2-537）；目标池那一行跟随的是最新那条（`_upsert_candidate`
写 `screening_id`）。`review_screening` 的「已复核不能再复核」（P2-736）只按这一条筛查判，复核结论又按（患者、病种）找池行就翻、
从不看池行跟随的是哪条：#2 确认并认领之后，复核旧的 #1「排除」照样 200，池行翻成排除、认领人与认领时间都还在——正是 P2-736
注释点名要挡的后果（修前实测）。

修法：池行跟随的是更新的一条筛查（`screening_id` 比这一条新）时，这一条算过时：409 点名那一条，不记复核、不动池行；确认 /
排除 / 待定同一判据。池行跟随的是这一条、更早的一条（居民自查不写池行、修前落下的旧记录）或没挂筛查号的，照旧。「池行没有跟随
更新的筛查」与翻池行压进同一条条件 UPDATE：判过之后新筛查才落进池里的，不翻（与先复核、后筛查同一个结果）。
"""
import contextlib

import pytest
from sqlalchemy import event as sa_event

from app.database import SessionLocal, engine
from app.spd.models import SpdCandidate, SpdScreening
from conftest import login

B = "/api/spd"
HIGH = {"family": "是", "salt": "是", "overweight": "是", "smoke": "否", "drink": "否", "symptom": "是"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21572 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    for name in ("a", "b"):
        created = client.post("/api/users", headers=admin, json={
            "username": f"p21572_{name}", "password": "passw0rd1", "full_name": f"P21572 医生{name}", "role": "doctor",
            "org_id": org})
        assert created.status_code in (200, 201), created.text
    return {"org": org, "a": login(client, "p21572_a", "passw0rd1"), "b": login(client, "p21572_b", "passw0rd1"),
            "n": 0}


def _patient(client, admin, world):
    world["n"] += 1
    made = client.post("/api/patients", headers=admin, json={
        "name": f"P21572 居民{world['n']}", "id_card": f"33010619700101{1572 + world['n']:04d}",
        "birth_date": "1970-01-01"})
    assert made.status_code in (200, 201), made.text
    patient = made.json()["id"]
    visit = client.post("/api/encounters", headers=world["a"], json={
        "patient_id": patient, "org_id": world["org"], "diagnosis_name": "头晕"})
    assert visit.status_code in (200, 201), visit.text
    return patient


def _screen(client, world, patient):
    resp = client.post(f"{B}/screenings", headers=world["a"], json={
        "patient_id": patient, "program_code": "hypertension", "scale_code": "scr_hypertension", "answers": HIGH})
    assert resp.status_code == 201 and resp.json()["result"] == "suspect", resp.text
    return resp.json()["id"]


def _legacy_screening(patient, org_id, source):
    """不经登记筛查接口落下的一条疑似（居民自查、修前的旧记录）：不写池行。"""
    with SessionLocal() as db:
        row = SpdScreening(patient_id=patient, program_code="hypertension", source=source, org_id=org_id,
                           scale_code="scr_hypertension", answers=HIGH, score=8, risk_level="high", result="suspect")
        db.add(row)
        db.commit()
        return row.id


def _review(client, headers, screening, result):
    return client.post(f"{B}/screenings/{screening}/review", headers=headers, json={"review_result": result})


def _pool(patient):
    with SessionLocal() as db:
        row = db.query(SpdCandidate).filter_by(patient_id=patient, program_code="hypertension").one()
        return {"id": row.id, "status": row.status, "user": row.assigned_user_id,
                "claimed_at": row.claimed_at, "screening_id": row.screening_id}


def _review_state(screening):
    with SessionLocal() as db:
        row = db.get(SpdScreening, screening)
        return row.reviewed, row.review_result, row.reviewer_id


@pytest.mark.parametrize("result", ["excluded", "confirmed", "pending"])
def test_新的一条已确认并认领_复核旧的一条409_池行不动(client, admin, world, result):
    patient = _patient(client, admin, world)
    old, new = _screen(client, world, patient), _screen(client, world, patient)
    assert _pool(patient)["screening_id"] == new   # 池行跟随最新那条
    assert _review(client, world["a"], new, "confirmed").status_code == 200
    claimed = client.post(f"{B}/candidates/{_pool(patient)['id']}/claim", headers=world["a"])
    assert claimed.status_code == 200, claimed.text
    before = _pool(patient)
    assert before["status"] == "target" and before["user"] is not None and before["claimed_at"] is not None

    stale = _review(client, world["b"], old, result)   # 医生乙的待复核清单里还挂着旧的 #1
    assert stale.status_code == 409, stale.text   # 修前 200
    assert stale.json() == {"detail": f"该居民已有更新的筛查 #{new}，池行跟随那一条，请复核那一条"}
    assert _pool(patient) == before   # 修前「排除」：池行翻成 excluded，认领人与认领时间还在
    assert _review_state(old) == (False, "", None)   # 不记复核


def test_只有一条筛查_照常复核翻池行(client, admin, world):
    patient = _patient(client, admin, world)
    only = _screen(client, world, patient)
    resp = _review(client, world["a"], only, "excluded")
    assert resp.status_code == 200 and resp.json()["review_result"] == "excluded", resp.text
    assert _pool(patient)["status"] == "excluded"


def test_两条疑似_复核池行跟随的新的那条照常(client, admin, world):
    patient = _patient(client, admin, world)
    _screen(client, world, patient)
    new = _screen(client, world, patient)
    resp = _review(client, world["a"], new, "excluded")
    assert resp.status_code == 200, resp.text
    assert _pool(patient)["status"] == "excluded"


def test_池行没挂筛查号的_照旧按复核结论翻(client, admin, world):
    """受理居民申请入池、直接建档入池的那一行不挂筛查号（`handle_service_apply` / `create_enrollment`）：无从判过时，照旧。"""
    patient = _patient(client, admin, world)
    screening = _screen(client, world, patient)
    with SessionLocal() as db:
        db.query(SpdCandidate).filter_by(patient_id=patient, program_code="hypertension").update({"screening_id": None})
        db.commit()
    resp = _review(client, world["a"], screening, "excluded")
    assert resp.status_code == 200, resp.text
    assert _pool(patient)["status"] == "excluded"


def test_池行跟随的是更早的一条_这一条不算过时(client, admin, world):
    """居民自查不写池行（机构为空，P1-229）：池行跟随的是更早的医护筛查，复核这条更新的自查照常——不是「不等于就过时」。"""
    patient = _patient(client, admin, world)
    first = _screen(client, world, patient)
    later = _legacy_screening(patient, None, "self")
    assert _pool(patient)["screening_id"] == first
    resp = _review(client, world["a"], later, "confirmed")
    assert resp.status_code == 200, resp.text
    assert _pool(patient)["status"] == "target"


@contextlib.contextmanager
def _newer_screening_lands_before_pool_write(candidate_id, newer_id):
    """下一条改目标池行的 UPDATE 发出之前，在同一条连接上让池行跟随一条更新的筛查（像 `_upsert_candidate` 那样回到疑似）。

    等价于 PG 上另一路登记筛查恰在「判过池行跟随的是这一条」之后、这条 UPDATE 之前提交（READ COMMITTED 逐语句取快照）；复核
    此前已翻了筛查那一行，SQLite 的库级写锁让另一个会话在这之间提交不了，时序钉法同 `test_spd_candidate_enrolled_race`。
    """
    fired: list = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if not fired and statement.lstrip().upper().startswith("UPDATE SPD_CANDIDATES"):
            fired.append(True)
            cursor.connection.execute("UPDATE spd_candidates SET status = 'suspect', screening_id = ? WHERE id = ?",
                                      (newer_id, candidate_id))

    sa_event.listen(engine, "before_cursor_execute", listener)
    try:
        yield fired
    finally:
        sa_event.remove(engine, "before_cursor_execute", listener)


def test_判过之后新筛查才落进池里_不翻池行(client, admin, world):
    patient = _patient(client, admin, world)
    screening = _screen(client, world, patient)
    newer = _legacy_screening(patient, world["org"], "opportunistic")   # 另一路刚登记、还没写池行的新筛查
    with _newer_screening_lands_before_pool_write(_pool(patient)["id"], newer) as fired:
        resp = _review(client, world["a"], screening, "excluded")
    assert fired
    assert resp.status_code == 200, resp.text   # 与「先复核、后筛查」同一个结果：复核照记
    pool = _pool(patient)
    assert (pool["status"], pool["screening_id"]) == ("suspect", newer)   # 修前翻成 excluded，池行却跟随新的那条
