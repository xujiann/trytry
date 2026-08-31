import pytest

from conftest import login


@pytest.fixture(scope="module")
def h(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def base(client, h):
    org = client.post("/api/organizations", json={"name": "县医院", "org_type": "lead_hospital", "level": "county"}, headers=h).json()
    patient = client.post("/api/patients", json={"name": "周强", "id_card": "320981199103034444"}, headers=h).json()
    return {"org": org, "patient": patient}


def test_report_template_and_amend(client, h, base):
    assert client.post("/api/exams/templates", json={"center_type": "imaging", "name": "胸部DR正常模板", "content": "两肺纹理清晰"}, headers=h).status_code == 201
    assert len(client.get("/api/exams/templates?center_type=imaging", headers=h).json()) == 1
    req = client.post("/api/exams", json={"patient_id": base["patient"]["id"], "from_org_id": base["org"]["id"], "center_type": "imaging", "item_code": "DR", "item_name": "胸部DR"}, headers=h).json()
    client.post(f"/api/exams/{req['id']}/claim", headers=h)
    rep = client.post(f"/api/exams/{req['id']}/report", json={"conclusion": "初步结论"}, headers=h).json()
    amended = client.patch(f"/api/exams/reports/{rep['id']}", json={"conclusion": "修订：未见异常"}, headers=h).json()
    assert amended["conclusion"].startswith("修订")


def test_cssd_request_fulfill(client, h, base):
    req = client.post("/api/cssd/requests", json={"org_id": base["org"]["id"], "item_name": "换药包", "quantity": 5}, headers=h).json()
    batch = client.post("/api/cssd/batches", json={"batch_no": "B-01", "center_org_id": base["org"]["id"], "item_name": "换药包", "quantity": 20}, headers=h).json()
    # 未灭菌批次不可发放
    assert client.post(f"/api/cssd/requests/{req['id']}/fulfill?batch_id={batch['id']}", headers=h).status_code == 409
    client.post(f"/api/cssd/batches/{batch['id']}/advance", headers=h)
    done = client.post(f"/api/cssd/requests/{req['id']}/fulfill?batch_id={batch['id']}", headers=h).json()
    assert done["status"] == "fulfilled"


def test_expert_blacklist_survey_triage(client, h, base):
    assert client.post("/api/consultations/experts", json={"name": "张主任", "org_id": base["org"]["id"], "specialty": "心内科"}, headers=h).status_code == 201
    assert len(client.get("/api/consultations/experts?available=true", headers=h).json()) == 1

    slot = client.post("/api/appointments/slots", json={"org_id": base["org"]["id"], "resource_type": "outpatient", "resource_name": "门诊", "slot_date": "2026-09-01", "capacity": 5}, headers=h).json()
    client.post("/api/appointments/blacklist", json={"patient_id": base["patient"]["id"], "reason": "多次爽约"}, headers=h)
    assert client.post("/api/appointments", json={"slot_id": slot["id"], "patient_id": base["patient"]["id"]}, headers=h).status_code == 403
    client.delete(f"/api/appointments/blacklist/{base['patient']['id']}", headers=h)
    assert client.post("/api/appointments", json={"slot_id": slot["id"], "patient_id": base["patient"]["id"]}, headers=h).status_code == 201

    client.post("/api/surveys", json={"target_type": "contract", "target_id": 1, "patient_id": base["patient"]["id"], "score": 5}, headers=h)
    client.post("/api/surveys", json={"target_type": "contract", "target_id": 1, "patient_id": base["patient"]["id"], "score": 4}, headers=h)
    stats = client.get("/api/surveys/stats?target_type=contract", headers=h).json()
    assert stats[0]["avg_score"] == 4.5

    triage = client.post("/api/triage/suggest", json=["胸痛", "心悸"], headers=h).json()
    assert triage["recommendations"][0]["department"] == "心血管内科"
    assert triage["emergency_hint"] is True


def test_health_articles_portal(client, h):
    a = client.post("/api/education/articles", json={"title": "高血压饮食指南", "category": "chronic", "content": "限盐限油"}, headers=h).json()
    # 未发布不可见
    assert client.get("/api/portal/health-articles").json() == []
    client.post(f"/api/education/articles/{a['id']}/publish", headers=h)
    articles = client.get("/api/portal/health-articles?category=chronic").json()
    assert articles[0]["title"] == "高血压饮食指南"
