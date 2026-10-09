"""慢专病转诊下转给当前持有机构自己：200、单子原地不动，再自己「随访接收」闭环（P2-1608，第四十七批扫描 AK4-3）。

`down_referral` 原先只查下转目标机构在不在。县医院接收（或登记到院）之后「下转」给县医院自己：200，`current_org`
不变、状态成了「已下转基层」；随访接收只认持有机构，于是同一家县医院再点一下就闭环、计入闭环率，`pt_ref_down`
下转承接积分记给县医院医生——患者根本没回基层。

修法：目标机构等于这张单此刻的持有机构（`current_org_id`，与 `_holds_case` 同一列）时 422，单子原地不动、轨迹与
承接任务一样都不落。全域角色代办同样按单子的持有机构判，不按操作人所在机构。同级之间（县 → 另一家县级）算不算
下转随 P2-481 待裁定，本条不拦。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"
DETAIL = "下转目标不能是当前接诊机构本身"


def _login(client, username):
    token = client.post("/api/auth/login", json={"username": username, "password": "pass123456"}).json()
    return {"Authorization": f"Bearer {token['access_token']}"}


@pytest.fixture(scope="module")
def world(client, admin):
    county = client.post("/api/organizations", headers=admin, json={
        "name": "P21608 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    town = client.post("/api/organizations", headers=admin, json={
        "name": "P21608 卫生院", "org_type": "township", "level": "township", "parent_id": county}).json()["id"]
    village = client.post("/api/organizations", headers=admin, json={
        "name": "P21608 村卫生室", "org_type": "village", "level": "village", "parent_id": town}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21608_county", "password": "pass123456", "role": "doctor", "org_id": county})
    assert created.status_code == 201, created.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21608 患者", "id_card": "330127196808081608"}).json()["id"]
    return {"county": county, "town": town, "village": village, "patient": patient,
            "cdoc": _login(client, "p21608_county")}


def _held_by_county(world, status):
    """一张县医院手上的单（已接收 / 已到院）：下转的两个合法前置态。"""
    from app.spd.models import SpdReferralCase

    with SessionLocal() as db:
        case = SpdReferralCase(patient_id=world["patient"], program_code="hypertension", direction="up",
                               initiator_org_id=world["village"], current_org_id=world["county"],
                               current_level="county", status=status, reason="P21608")
        db.add(case)
        db.commit()
        return case.id


def _row(case_id):
    """单子的状态 / 持有机构 / 目标机构、本单轨迹条数、这位患者名下的下转承接任务条数。"""
    from app.spd.models import SpdReferralCase, SpdReferralStep, SpdTask

    with SessionLocal() as db:
        case = db.get(SpdReferralCase, case_id)
        steps = db.query(SpdReferralStep).filter(SpdReferralStep.case_id == case_id).count()
        tasks = db.query(SpdTask).filter(SpdTask.patient_id == case.patient_id,
                                         SpdTask.title == "下转承接与随访").count()
        return case.status, case.current_org_id, case.target_org_id, steps, tasks


@pytest.mark.parametrize("status", ["accepted", "arrived"])
def test_县医院下转给自己_422_单子原地不动(client, world, status):
    case_id = _held_by_county(world, status)
    before = _row(case_id)
    got = client.post(f"{B}/referrals/{case_id}/down", headers=world["cdoc"],
                      json={"target_org_id": world["county"], "stable": True})
    assert got.status_code == 422 and got.json()["detail"] == DETAIL, got.text   # 修前 200、状态成了「已下转基层」
    assert _row(case_id) == before
    assert before[:3] == (status, world["county"], None)


def test_全域角色代办也按单子的持有机构判(client, admin, world):
    """admin 不绑机构：判据是这张单的 `current_org_id`，不是操作人所在机构。"""
    case_id = _held_by_county(world, "arrived")
    got = client.post(f"{B}/referrals/{case_id}/down", headers=admin, json={"target_org_id": world["county"]})
    assert got.status_code == 422 and got.json()["detail"] == DETAIL, got.text
    assert _row(case_id)[0] == "arrived"


@pytest.mark.parametrize("target", ["village", "town"])
def test_下转给村卫生室或卫生院照旧200(client, world, target):
    case_id = _held_by_county(world, "arrived")
    tasks_before = _row(case_id)[4]
    got = client.post(f"{B}/referrals/{case_id}/down", headers=world["cdoc"],
                      json={"target_org_id": world[target], "stable": True})
    assert got.status_code == 200, got.text
    body = got.json()
    assert (body["status"], body["current_org_id"], body["target_org_id"]) == (
        "down_referred", world[target], world[target])
    assert _row(case_id) == ("down_referred", world[target], world[target], 1, tasks_before + 1)
