"""转诊审核时改的目标机构不查在不在：填错一个编号，外键撞约束即 500（P2-301）。

`review_referral` 审核通过时照写 `body.target_org_id`；发起转诊、转诊规则建 / 改档都先查「目标机构不存在」，
审核这一处没有。`spd_referral_cases.target_org_id` 是外键：真 PG 上撞约束 500，测试库开着外键同样抛错。

修法：审核通过时带了目标机构的先查，不存在 404（与发起同一句），单子原地不动。驳回不用这个字段，不查。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    county = client.post("/api/organizations", headers=admin, json={
        "name": "P2301 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    village = client.post("/api/organizations", headers=admin, json={
        "name": "P2301 村卫生室", "org_type": "village", "level": "village"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2301 患者", "id_card": "330127197309092301"}).json()["id"]
    return {"county": county, "village": village, "patient": patient}


def _submitted(world):
    from app.spd.models import SpdReferralCase

    with SessionLocal() as db:
        case = SpdReferralCase(patient_id=world["patient"], program_code="hypertension", direction="up",
                               initiator_org_id=world["village"], current_org_id=world["village"],
                               current_level="village", status="submitted", reason="P2301")
        db.add(case)
        db.commit()
        return case.id


def _row(case_id):
    from app.spd.models import SpdReferralCase

    with SessionLocal() as db:
        case = db.get(SpdReferralCase, case_id)
        return case.status, case.target_org_id


def test_审核通过带不存在的目标机构_404_单子原地不动(client, admin, world):
    case_id = _submitted(world)
    got = client.post(f"{B}/referrals/{case_id}/review", headers=admin,
                      json={"action": "pass", "target_org_id": 99999999})
    assert got.status_code == 404 and got.json()["detail"] == "目标机构不存在", got.text   # 修前撞外键
    assert _row(case_id) == ("submitted", None)


def test_目标机构存在照常推进_驳回不看这个字段(client, admin, world):
    case_id = _submitted(world)
    ok = client.post(f"{B}/referrals/{case_id}/review", headers=admin,
                     json={"action": "pass", "target_org_id": world["county"]})
    assert ok.status_code == 200, ok.text
    assert _row(case_id) == ("township_reviewed", world["county"])
    rejected = client.post(f"{B}/referrals/{_submitted(world)}/review", headers=admin,
                           json={"action": "reject", "target_org_id": 99999999})
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected", rejected.text
