"""收缩压 ≤ 舒张压照收：慢病随访录反一次就定 3 级、建议上转（P2-1016，第二十九批「字段之间的约束」扫描 E3-1）。

`FollowUpCreate` 的 sbp / dbp 各自只有上下界、两者之间不比：135/85 录成 85/135，舒张压 135 按「越高越危」定 3 级「高危需
转诊评估」、`refer_up_suggested=true`，档案级别被改写；120/120 同样定 3 级。住院体温单 70/150、急救途中体征同形。与 P1-101 /
P1-214「生理上不可能的测量值不收」同一条规矩：两项都测到（都大于 0）时收缩压须高于舒张压，否则 422。急救心跳骤停记 0/0 照收。
"""
import pytest

from app.vitals import bp_order_problem


@pytest.mark.parametrize("sbp, dbp, bad", [
    (135, 85, False), (85, 135, True), (120, 120, True), (0, 0, False), (None, 80, False), (120, None, False),
    (0, 60, False),   # 0 不参与比较，由各入口自己的下界挡
])
def test_判据(sbp, dbp, bad):
    assert bool(bp_order_problem(sbp, dbp)) is bad


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21016 县医院", "org_type": "lead_hospital", "level": "county"}).json()
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21016 高血压", "id_card": "330106197003031016", "gender": "男", "birth_date": "1970-03-03"}).json()
    chronic = client.post("/api/chronic", headers=admin, json={
        "patient_id": patient["id"], "disease": "hypertension", "managed_by_org_id": org["id"]}).json()
    return {"org": org, "patient": patient, "chronic": chronic}


def _level(client, admin, chronic_id):
    return next(c for c in client.get("/api/chronic?limit=500", headers=admin).json() if c["id"] == chronic_id)["level"]


@pytest.mark.parametrize("body", [
    {"sbp": 85, "dbp": 135},
    {"sbp": 120, "dbp": 120},
    {"metrics": {"sbp": 85, "dbp": 135}},    # 列为空取 metrics 同名键，与分级同一口径
    {"sbp": 85, "metrics": {"dbp": 135}},
])
def test_慢病随访倒挂的血压拒收_分级不动(client, admin, world, body):
    cid = world["chronic"]["id"]
    before = _level(client, admin, cid)
    got = client.post(f"/api/chronic/{cid}/followups", headers=admin, json=body)
    assert got.status_code == 422, got.text   # 修前 201、定 3 级、建议上转
    assert "收缩压须高于舒张压" in got.json()["detail"]
    assert _level(client, admin, cid) == before


def test_慢病随访正常的血压照收(client, admin, world):
    got = client.post(f"/api/chronic/{world['chronic']['id']}/followups", headers=admin, json={"sbp": 135, "dbp": 85})
    assert got.status_code == 201, got.text
    assert (got.json()["level"], got.json()["refer_up_suggested"]) == (1, False)


def test_对接入站的血压倒挂同样拒收(client, admin, world):
    obs = {
        "resourceType": "Observation", "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
        "subject": {"reference": f"Patient/{world['patient']['ehc_no']}"},
        "component": [
            {"code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]}, "valueQuantity": {"value": 85}},
            {"code": {"coding": [{"system": "http://loinc.org", "code": "8462-4"}]}, "valueQuantity": {"value": 135}},
        ],
    }
    before = len(client.get(f"/api/chronic/{world['chronic']['id']}/followups", headers=admin).json())
    got = client.post("/api/integration/fhir/Observation", headers=admin, json=obs)
    assert got.status_code == 422, got.text
    assert len(client.get(f"/api/chronic/{world['chronic']['id']}/followups", headers=admin).json()) == before


def test_住院体温单血压倒挂拒收(client, admin, world):
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": world["org"]["id"], "name": "P21016 病区"}).json()
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward["id"], "bed_no": "P21016"}).json()
    adm = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": world["patient"]["id"], "ward_id": ward["id"], "bed_id": bed["id"], "diagnosis_name": "高血压"}).json()
    path = f"/api/inpatient/admissions/{adm['id']}/vitals"
    bad = client.post(path, headers=admin, json={"measured_at": "2026-09-30 08:00", "sbp": 70, "dbp": 150})
    assert bad.status_code == 422, bad.text   # 修前 201
    ok = client.post(path, headers=admin, json={"measured_at": "2026-09-30 08:00", "sbp": 150, "dbp": 70})
    assert ok.status_code == 201, ok.text


def test_急救途中体征倒挂拒收_心跳骤停记零照收(client, admin, world):
    case = client.post("/api/emergency/cases", headers=admin, json={
        "location": "P21016 路口", "symptom": "胸痛", "ambulance_no": "浙B1016",
        "dest_org_id": world["org"]["id"], "channel_type": "chest_pain"}).json()
    path = f"/api/emergency/cases/{case['id']}/vitals"
    assert client.post(path, headers=admin, json={"sbp": 70, "dbp": 150}).status_code == 422
    assert client.post(path, headers=admin, json={"heart_rate": 0, "sbp": 0, "dbp": 0}).status_code == 201
    assert client.post(path, headers=admin, json={"sbp": 150, "dbp": 70}).status_code == 201
