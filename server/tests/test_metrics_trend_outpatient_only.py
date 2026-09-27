"""驾驶舱「近 6 月业务量趋势」的「就诊」不含住院类就诊记录（P2-642，第十四批「单位与量纲」扫描 R3-8）。

办入院会同时建一条 `encounter_type="inpatient"` 的就诊记录；同页卡片的诊疗人次早已排除它（P2-199，`OUTPATIENT_ENCOUNTER`），
趋势照数——一次门诊加一次入院，卡片 1、趋势 2，同一位住院患者在趋势里算一次就诊。修后与卡片同一条口径。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2642 医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2642病区"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "1"}).json()["id"]
    patients = [client.post("/api/patients", headers=admin, json={
        "name": f"P2642 患者{i}", "id_card": f"33028119800303264{i}"}).json()["id"] for i in range(2)]
    return {"org": org, "ward": ward, "bed": bed, "patients": patients}


def _this_month_encounters(client, admin):
    body = client.get("/api/metrics/trends?months=1", headers=admin).json()
    return body["series"]["encounters"][-1]


def test_入院建的住院就诊不算进趋势的就诊(client, admin, world):
    before = _this_month_encounters(client, admin)
    assert client.post("/api/encounters", headers=admin, json={
        "patient_id": world["patients"][0], "org_id": world["org"], "diagnosis_name": "感冒"}).status_code == 201
    admitted = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": world["patients"][1], "ward_id": world["ward"], "bed_id": world["bed"], "diagnosis_name": "肺炎"})
    assert admitted.status_code == 201, admitted.text
    assert _this_month_encounters(client, admin) - before == 1   # 修前 2：入院那条住院类就诊也算了
