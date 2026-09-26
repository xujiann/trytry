"""病历质控回执的「参与规则 N 条」只数这次真正参与评分的规则（P2-203）。

`evaluate_record` 对带触发条件、条件没触发的规则明写「本次不参与评分（不计分母也不扣分）」，回执的 `rules_checked`
却照数全部启用规则：没有危急值、没出院的患者，「危急值须有处置记录」「出院须有病案首页」两条都跳过了，回执照印
「参与规则 12 条」。条件触发时（患者有危急值报告）那一条要算进来。
"""
import pytest

from conftest import login

RECORD = {
    "chief_complaint": "乏力3天", "present_illness": "3天前无明显诱因出现乏力，伴纳差，无发热，无胸痛，未诊治。",
    "past_history": "否认高血压糖尿病史", "physical_exam": "血压120/80mmHg，心肺未见异常",
    "diagnosis_basis": "乏力伴纳差，查血钾偏高", "treatment_plan": "降钾治疗，复查电解质",
}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2203 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    client.post("/api/users", headers=admin, json={
        "username": "p2203_doc", "password": "pass123456", "role": "doctor", "org_id": org, "full_name": "P2203 医师"})
    doctor = login(client, "p2203_doc", "pass123456")
    patients = [client.post("/api/patients", headers=admin, json={
        "name": f"P2203 患者{n}", "id_card": f"33010619770707{1590 + n}"}).json()["id"] for n in range(2)]
    return {"org": org, "doctor": doctor, "patients": patients}


def _record(client, world, patient_id):
    enc = client.post("/api/encounters", headers=world["doctor"], json={
        "patient_id": patient_id, "org_id": world["org"], "doctor_name": "P2203 医师", "diagnosis_name": "高钾血症"}).json()
    resp = client.post("/api/quality/records", headers=world["doctor"], json={"encounter_id": enc["id"], **RECORD})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["qc"]


def test_条件没触发的规则不算参与(client, admin, world):
    active = [r for r in client.get("/api/quality/record-qc-rules", headers=admin).json() if r["active"]]
    conditional = [r for r in active if (r.get("config") or {}).get("condition")]
    assert len(conditional) == 2
    qc = _record(client, world, world["patients"][0])
    assert qc["rules_checked"] == len(active) - 2                  # 修前 = len(active)


def test_有危急值报告的患者_危急值那一条参与评分(client, admin, world):
    patient = world["patients"][1]
    req = client.post("/api/exams", headers=world["doctor"], json={
        "patient_id": patient, "from_org_id": world["org"], "center_type": "lab",
        "item_code": "K", "item_name": "血清钾测定"}).json()
    client.post(f"/api/exams/{req['id']}/claim", headers=world["doctor"])
    rep = client.post(f"/api/exams/{req['id']}/report", headers=world["doctor"], json={
        "finding": "血清钾 6.9 mmol/L", "conclusion": "重度高钾血症", "critical": True})
    assert rep.status_code in (200, 201), rep.text
    active = [r for r in client.get("/api/quality/record-qc-rules", headers=admin).json() if r["active"]]
    assert _record(client, world, patient)["rules_checked"] == len(active) - 1   # 只剩「出院须有病案首页」不参与
