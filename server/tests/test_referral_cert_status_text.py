"""被退回的转诊开转诊证明，报的却是「转诊尚未接诊」（P2-413）。

签证明只认已接诊 / 已结案的转诊，其余一律 409「转诊尚未接诊，不可签发证明」——被接收方退回的转诊也这么说，
经办以为再等等对方接诊就能签，其实这条转诊已经走不下去了。修后状态文案取自转诊模块的 `STATUS_LABELS`
（与转诊页同一份）：待接诊那句原样不动，已退回的说「转诊已退回」。
"""
import pytest

from app.database import SessionLocal
from app.models import Referral


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": f"P2413 {n}", "org_type": t, "level": lv}).json()["id"]
        for n, t, lv in (("卫生院", "township", "township"), ("县医院", "lead_hospital", "county"))]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2413 患者", "id_card": "330127197309092413"}).json()["id"]
    return {"orgs": orgs, "patient": patient}


def _referral(client, admin, world, status):
    created = client.post("/api/referrals", headers=admin, json={
        "patient_id": world["patient"], "from_org_id": world["orgs"][0], "to_org_id": world["orgs"][1],
        "direction": "up", "reason": "P2413 上转"})
    assert created.status_code in (200, 201), created.text
    rid = created.json()["id"]
    if status != "pending":
        with SessionLocal() as db:
            db.get(Referral, rid).status = status
            db.commit()
    return rid


def test_被退回的转诊_说已退回而不是尚未接诊(client, admin, world):
    rid = _referral(client, admin, world, "rejected")
    got = client.post(f"/api/insurance/referral-certs/{rid}", headers=admin)
    assert (got.status_code, got.json()["detail"]) == (409, "转诊已退回，不可签发证明"), got.text   # 修前「尚未接诊」


def test_待接诊的文案不变(client, admin, world):
    rid = _referral(client, admin, world, "pending")
    got = client.post(f"/api/insurance/referral-certs/{rid}", headers=admin)
    assert (got.status_code, got.json()["detail"]) == (409, "转诊尚未接诊，不可签发证明"), got.text
