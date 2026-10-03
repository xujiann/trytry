"""转诊「审核超时预警」只数还在审核环节的单子（P2-1192，第三十四批「跨机构协作的两端」扫描 L2-5）。

`GET /api/spd/referrals-alerts` 的说明写「转诊审核超时预警（医生移动端 #19）」，需求对照表医生移动端 #19 写「展示转诊审核
超时」；实现却按 `status NOT IN (closed, rejected, withdrawn)` 数：已接收待到院、已到院、已下转的都算「超时未推进」。到院之后
的下一步是「病情稳定再下转」，没有 48 小时时限——凡是住院超过 48 小时的上转患者都进了县医院、村卫生室的督办清单与计数，
清单按最近推进时间升序，住得越久越靠前，真卡在审核环节的单子被挤到后面。医生移动端工作台的「超时督办」同一口径。
修前实测（scan34 l2/r7，一张卡在「已发起」3 天、两张已到院 3 天）：县人民医院 `count=2 第一页=[(2,'arrived')]`、村卫生室
`count=3`、县医院移动端超时督办 2。

修法：两处只数审核环节——待卫生院审核（`submitted`，含收敛前存量的 `station_reviewed`）、待县级医院接收
（`township_reviewed`），即状态机 `_NEXT` 的键（`service.REFERRAL_REVIEW_STATUSES`，两处共用）。
同一个接口顺带（P2-1157 漏的一处）：逐行 `_case_out` 每行再 `db.get` 一次患者，照 `/api/spd/referrals` 的写法按页一次 IN
取齐（`deps.rows_by_id`），响应不变；照 `test_paged_list_query_count.py` 数 SQL 条数，页大小 2 与 12 两档相等。
"""
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import event

from app.clock import now_naive
from app.database import SessionLocal, engine
from app.models import Organization, Patient
from app.spd.models import SpdReferralCase, SpdReferralStep
from app.spd.routers import referral as spd_referral
from app.spd.service import REFERRAL_REVIEW_STATUSES
from conftest import login

B = "/api/spd"
N = 12
#: 每张单走到哪一态，以及各条环节轨迹（发起在前）落在几小时前：最近一次推进都在三天前
STALLED = {
    "submitted": [80],
    "township_reviewed": [90, 75],
    "accepted": [90, 85, 74],
    "arrived": [90, 85, 80, 73],
    "down_referred": [90, 85, 80, 75, 72],
}


def _backdate(case_id, steps_hours_ago):
    """把单子的建单时间与环节轨迹（按先后）挪到若干小时前。"""
    now = now_naive()
    with SessionLocal() as db:
        db.get(SpdReferralCase, case_id).created_at = now - timedelta(hours=steps_hours_ago[0])
        steps = db.query(SpdReferralStep).filter(SpdReferralStep.case_id == case_id).order_by(SpdReferralStep.id).all()
        assert len(steps) == len(steps_hours_ago), case_id
        for step, hours in zip(steps, steps_hours_ago):
            step.created_at = now - timedelta(hours=hours)
        db.commit()


@pytest.fixture(scope="module")
def world(client, admin):
    orgs: dict[str, int] = {}
    for key, name, level, org_type, parent in (
        ("county", "P1192 县人民医院", "county", "lead_hospital", None),
        ("town", "P1192 卫生院", "township", "township", "county"),
        ("village", "P1192 村卫生室", "village", "village", "town"),
    ):
        body = {"name": name, "level": level, "org_type": org_type}
        if parent:
            body["parent_id"] = orgs[parent]
        resp = client.post("/api/organizations", headers=admin, json=body)
        assert resp.status_code == 201, resp.text
        orgs[key] = resp.json()["id"]
    heads = {}
    for key in ("county", "town", "village"):
        username = f"p1192_{key}"
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "full_name": username, "role": "doctor",
            "org_id": orgs[key]})
        assert resp.status_code in (200, 201), resp.text
        heads[key] = login(client, username, "passw0rd1")
    chain = [("town", "review", {"action": "pass"}), ("county", "review", {"action": "pass"}),
             ("county", "arrive", {"effective_visit": True}), ("county", "down", {"target_org_id": orgs["town"]})]
    cases = {}
    for i, (key, hours) in enumerate(STALLED.items()):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P1192 患者{i}", "id_card": f"33012719620303119{i}"}).json()["id"]
        enrolled = client.post(f"{B}/enrollments", headers=admin, json={
            "patient_id": patient, "program_code": "hypertension", "org_id": orgs["village"]})
        assert enrolled.status_code == 201, enrolled.text
        resp = client.post(f"{B}/referrals", headers=heads["village"], json={
            "patient_id": patient, "program_code": "hypertension", "target_org_id": orgs["county"],
            "reason": f"P1192 {key}"})
        assert resp.status_code == 201, resp.text
        case_id = resp.json()["id"]
        for who, path, body in chain[:len(hours) - 1]:
            resp = client.post(f"{B}/referrals/{case_id}/{path}", headers=heads[who], json=body)
            assert resp.status_code == 200, (key, path, resp.text)
        assert resp.json()["status"] == key
        _backdate(case_id, hours)
        cases[key] = case_id
    return {"orgs": orgs, "heads": heads, "cases": cases}


def _alerts(client, headers, **params):
    resp = client.get(f"{B}/referrals-alerts", headers=headers, params={"hours": 48, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_审核环节的状态就是状态机里待审核的那几态():
    assert set(REFERRAL_REVIEW_STATUSES) == set(spd_referral._NEXT)


def test_到院三天的不算审核超时_卡在发起与待接收的照报(client, admin, world):
    cases = world["cases"]
    review_stalled = [cases["submitted"], cases["township_reviewed"]]   # 最久没动的在前
    body = _alerts(client, world["heads"]["village"])   # 村卫生室是发起机构，五张都看得见
    assert [c["id"] for c in body["items"]] == review_stalled   # 修前已接收 / 已到院 / 已下转的也在
    assert body["count"] == 2   # 修前 5
    body = _alerts(client, world["heads"]["county"])   # 县医院手上的是已接收、已到院的：不在审核环节，没有 48 小时时限
    assert (body["items"], body["count"]) == ([], 0)   # 修前 count=2、第一页是已到院的
    body = _alerts(client, world["heads"]["town"])   # 卫生院手上：自己审过、等县里接收的那张照报；下转过来的不算
    assert ([c["id"] for c in body["items"]], body["count"]) == ([cases["township_reviewed"]], 1)   # 修前 2
    seen = {c["id"] for c in _alerts(client, admin, limit=500)["items"]}
    assert seen & set(cases.values()) == set(review_stalled)


def test_医生移动端超时督办同一口径(client, world):
    def overdue(who):
        resp = client.get(f"{B}/workbench/doctor-mobile", headers=world["heads"][who])
        assert resp.status_code == 200, resp.text
        return resp.json()["referrals"]["overdue"]

    assert overdue("county") == 0   # 修前 2
    assert overdue("village") == 2   # 修前 5
    assert overdue("town") == 1   # 修前 2


# ------------------------------------------------------------------ 逐行查患者（P2-1157 漏的一处）


@contextmanager
def count_sql():
    counter = {"n": 0}

    def _tick(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _tick)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _tick)


@pytest.fixture(scope="module")
def backlog(client, admin):
    """另一家机构积压 N 张卡在「已发起」的单子，患者各不相同，逐行查询躲不进身份映射。"""
    with SessionLocal() as db:
        org = Organization(name="P1192 积压卫生院", org_type="township", level="township")
        db.add(org)
        db.flush()
        patients = [Patient(name=f"P1192 积压患者{i}", id_card=f"33012719630404{i:04d}", ehc_no=f"EHC-P1192-{i}")
                    for i in range(N)]
        db.add_all(patients)
        db.flush()
        old = now_naive() - timedelta(days=5)
        db.add_all([SpdReferralCase(patient_id=p.id, program_code="hypertension", initiator_org_id=org.id,
                                    current_org_id=org.id, current_level="township", status="submitted",
                                    created_at=old + timedelta(minutes=i))
                    for i, p in enumerate(patients)])
        db.commit()
        org_id = org.id
    resp = client.post("/api/users", headers=admin, json={
        "username": "p1192_backlog", "password": "passw0rd1", "full_name": "p1192_backlog", "role": "doctor",
        "org_id": org_id})
    assert resp.status_code in (200, 201), resp.text
    return login(client, "p1192_backlog", "passw0rd1")


def test_超时预警的SQL条数与页大小无关(client, backlog):
    counts = {}
    for limit in (2, N):
        _alerts(client, backlog, limit=limit)   # 预热
        with count_sql() as sql:
            body = _alerts(client, backlog, limit=limit)
        assert (len(body["items"]), body["count"]) == (limit, N)
        counts[limit] = sql["n"]
    assert counts[N] == counts[2], f"页大小 2 → {N}，SQL 从 {counts[2]} 条涨到 {counts[N]} 条——又逐行查患者了"


def test_超时预警的行与单条出参一致(client, backlog):
    """单条出参那条路（逐行 `db.get` 患者）没动，拿它当原实现逐行对照；键序一并比。"""
    items = _alerts(client, backlog, limit=N)["items"]
    with SessionLocal() as db:
        want = [spd_referral.ReferralCaseOut.model_validate(
            spd_referral._case_out(db, db.get(SpdReferralCase, row["id"]))).model_dump(mode="json") for row in items]
    assert [list(row) for row in items] == [list(row) for row in want]
    assert items == want
    assert [row["patient_name"] for row in items] == [f"P1192 积压患者{i}" for i in range(N)]   # 最久没动的在前
