"""有序转诊率不数挂上之后被退回的转诊单（P2-200）。

登记县外就诊时已拒绝挂退回的转诊单（「被退回的转诊单没有转成，患者是自行外出——挂上去就算成了有序转诊」），
可挂的时候还是待审、之后才被退回的，原先一直算数：县外就诊没有改挂 / 解挂的入口，有序转诊率（医共体的头条指标）
就一直停在 50%。同文件的原则是「统计是现算的，之后被退回自然就掉出去」。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {}
    for key, name, otype, level in (("town", "P2200 卫生院", "township", "township"),
                                    ("county", "P2200 县医院", "lead_hospital", "county")):
        orgs[key] = client.post("/api/organizations", headers=admin,
                                json={"name": name, "org_type": otype, "level": level}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2200 患者", "id_card": "330106196606061564"}).json()["id"]
    referral = client.post("/api/referrals", headers=admin, json={
        "patient_id": patient, "from_org_id": orgs["town"], "to_org_id": orgs["county"], "direction": "up"}).json()
    for referral_id, date in ((referral["id"], "2026-08-05"), (None, "2026-08-09")):
        resp = client.post("/api/analytics/outbound-visits", headers=admin, json={
            "patient_id": patient, "visit_date": date, "external_org_name": "市第一人民医院",
            "referral_id": referral_id})
        assert resp.status_code == 201, resp.text
    return referral["id"]


def _flow(client, admin):
    return client.get("/api/analytics/patient-flow", headers=admin).json()


def test_挂上后被退回_有序转诊率随之掉下来(client, admin, world):
    before = _flow(client, admin)
    assert (before["referred_outbound"], before["ordered_referral_rate_pct"]) == (1, 50.0)
    resp = client.patch(f"/api/referrals/{world}/status", headers=admin, json={"status": "rejected"})
    assert resp.status_code == 200, resp.text
    after = _flow(client, admin)
    assert (after["referred_outbound"], after["ordered_referral_rate_pct"]) == (0, 0.0)   # 修前仍是 1 / 50.0
    assert after["outside_visits"] == 2                                                   # 县外就诊本身照数
