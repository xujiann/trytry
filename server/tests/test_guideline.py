"""功能指引补齐模块测试：⑦⑨⑬⑭⑮⑯⑲⑳㉑㉓㉔㉕㉖㉗㉘㉚㉛㉜㉞及排班/质控/样本物流。"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def h(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def base(client, h):
    county = client.post("/api/organizations", json={"name": "县医院", "org_type": "lead_hospital", "level": "county"}, headers=h).json()
    township = client.post("/api/organizations", json={"name": "南镇卫生院", "org_type": "township", "level": "township"}, headers=h).json()
    patient = client.post("/api/patients", json={"name": "孙丽", "id_card": "320981198812124321", "gender": "女"}, headers=h).json()
    return {"county": county, "township": township, "patient": patient}


def test_07_emergency_case_flow(client, h, base):
    case = client.post("/api/emergency/cases", json={"location": "南镇集市", "symptom": "胸痛倒地", "ambulance_no": "苏J-120-01", "dest_org_id": base["county"]["id"]}, headers=h).json()
    assert case["status"] == "dispatched"
    vital = client.post(f"/api/emergency/cases/{case['id']}/vitals", json={"heart_rate": 118, "sbp": 88, "dbp": 56, "spo2": 91, "note": "疑似心源性休克"}, headers=h)
    assert vital.status_code == 201
    for expected in ("en_route", "arrived", "admitted"):
        assert client.post(f"/api/emergency/cases/{case['id']}/advance", headers=h).json()["status"] == expected
    # 收治后不再接收院前体征
    assert client.post(f"/api/emergency/cases/{case['id']}/vitals", json={"heart_rate": 100}, headers=h).status_code == 409


def test_09_telemedicine_repeat_rx(client, h, base):
    rx = client.post("/api/prescriptions", json={"patient_id": base["patient"]["id"], "org_id": base["township"]["id"], "diagnosis_name": "高血压", "items": [{"drug_code": "AMLODIPINE", "drug_name": "氨氯地平", "daily_dose": 5, "days": 30}]}, headers=h).json()
    consult = client.post("/api/telemedicine/consults", json={"patient_id": base["patient"]["id"], "org_id": base["township"]["id"], "consult_type": "repeat_rx", "question": "血压平稳，申请续方"}, headers=h).json()
    replied = client.post(f"/api/telemedicine/consults/{consult['id']}/reply", json={"reply": "同意续方30天", "doctor_name": "王医生", "prescription_id": rx["id"]}, headers=h).json()
    assert replied["status"] == "replied" and replied["prescription_id"] == rx["id"]
    assert client.post(f"/api/telemedicine/consults/{consult['id']}/close", headers=h).json()["status"] == "closed"


def test_13_tcm_assist(client, h):
    constitution = client.post("/api/tcm/constitution", json={"scores": {"qi_deficiency": 65, "yin_deficiency": 30}}, headers=h).json()
    assert constitution["constitution"] == "气虚质"
    diag = client.post("/api/tcm/assist-diagnosis", json={"symptoms": ["乏力", "气短", "口干"]}, headers=h).json()
    assert diag["recommendations"][0]["syndrome"] == "气虚证"
    assert diag["recommendations"][0]["match_count"] == 2


def test_14_tcm_dispense_trace(client, h, base):
    order = client.post("/api/tcm/dispense-orders", json={"patient_id": base["patient"]["id"], "from_org_id": base["township"]["id"], "herbs": "黄芪30g 白术15g 防风10g", "doses": 7, "decoct": True}, headers=h).json()
    statuses = []
    for _ in range(4):
        statuses.append(client.post(f"/api/tcm/dispense-orders/{order['id']}/advance", headers=h).json()["status"])
    assert statuses == ["dispensed", "decocted", "delivering", "delivered"]
    assert client.post(f"/api/tcm/dispense-orders/{order['id']}/advance", headers=h).status_code == 409


def test_15_16_shortage_and_profile(client, h, base):
    shortage = client.post("/api/medication/shortages", json={"org_id": base["township"]["id"], "drug_code": "INSULIN", "drug_name": "胰岛素", "quantity": 10}, headers=h).json()
    assert client.post(f"/api/medication/shortages/{shortage['id']}/advance", headers=h).json()["status"] == "purchasing"
    assert client.post(f"/api/medication/shortages/{shortage['id']}/advance", headers=h).json()["status"] == "delivered"

    profile = client.get(f"/api/medication/profile/{base['patient']['id']}", headers=h).json()
    assert profile["distinct_drugs"] == 1 and profile["polypharmacy_warning"] is False
    stats = client.get("/api/medication/usage-stats", headers=h).json()
    assert stats[0]["drug_code"] == "AMLODIPINE"


def test_19_insurance(client, h, base):
    bad = client.post("/api/insurance/settlements", json={"patient_id": base["patient"]["id"], "org_id": base["township"]["id"], "total_amount": 100, "insurance_pay": 70, "self_pay": 20}, headers=h)
    assert bad.status_code == 422
    client.post("/api/insurance/settlements", json={"patient_id": base["patient"]["id"], "org_id": base["township"]["id"], "total_amount": 100, "insurance_pay": 70, "self_pay": 30}, headers=h)
    client.post("/api/insurance/settlements", json={"patient_id": base["patient"]["id"], "org_id": base["county"]["id"], "settle_type": "remote", "total_amount": 200, "insurance_pay": 120, "self_pay": 80}, headers=h)
    stats = client.get("/api/insurance/fund-stats", headers=h).json()
    assert stats["insurance_pay_total"] == 190.0
    assert stats["grassroots_ratio_pct"] == round(70 * 100 / 190, 2)

    ref = client.post("/api/referrals", json={"patient_id": base["patient"]["id"], "from_org_id": base["township"]["id"], "to_org_id": base["county"]["id"], "direction": "up", "reason": "上转"}, headers=h).json()
    assert client.post(f"/api/insurance/referral-certs/{ref['id']}", headers=h).status_code == 409  # 未接诊
    client.patch(f"/api/referrals/{ref['id']}/status", json={"status": "accepted"}, headers=h)
    cert = client.post(f"/api/insurance/referral-certs/{ref['id']}", headers=h).json()
    assert cert["cert_no"].startswith("ZZ")
    assert client.post(f"/api/insurance/referral-certs/{ref['id']}", headers=h).json()["cert_no"] == cert["cert_no"]

    app_ = client.post("/api/insurance/special-diseases", json={"patient_id": base["patient"]["id"], "disease_name": "尿毒症透析"}, headers=h).json()
    assert client.post(f"/api/insurance/special-diseases/{app_['id']}/review?approve=true", headers=h).json()["status"] == "approved"


def test_20_21_education_and_techniques(client, h):
    course = client.post("/api/education/courses", json={"title": "基层心电图判读", "course_type": "live", "speaker": "县院心内科"}, headers=h).json()
    result = client.post(f"/api/education/courses/{course['id']}/exam", json={"score": 55}, headers=h).json()
    assert result["passed"] is False
    result = client.post(f"/api/education/courses/{course['id']}/exam", json={"score": 88}, headers=h).json()
    assert result["passed"] is True and result["score"] == 88
    stats = client.get(f"/api/education/courses/{course['id']}/stats", headers=h).json()
    assert stats["trainees"] == 1 and stats["pass_rate_pct"] == 100.0

    tech = client.post("/api/tcm/techniques", json={"name": "艾灸足三里", "category": "灸法", "indication": "脾胃虚弱、气虚乏力"}, headers=h)
    assert tech.status_code == 201
    found = client.get("/api/tcm/techniques?keyword=气虚", headers=h).json()
    assert len(found) == 1


def test_23_eldercare_grading(client, h, base):
    a1 = client.post("/api/eldercare/assessments", json={"patient_id": base["patient"]["id"], "adl_score": 55, "cognitive_score": 20, "assessed_date": "2026-08-01"}, headers=h).json()
    assert a1["care_level"] == "中度失能"
    a2 = client.post("/api/eldercare/assessments", json={"patient_id": base["patient"]["id"], "adl_score": 96, "assessed_date": "2026-08-10"}, headers=h).json()
    assert a2["care_level"] == "能力完好"
    # 取最新评估：已恢复，不在失能清单
    assert client.get("/api/eldercare/disabled", headers=h).json() == []


def test_24_maternal_and_child(client, h, base):
    record = client.post("/api/maternal/records", json={"patient_id": base["patient"]["id"], "lmp": "2026-05-01", "edc": "2027-02-05"}, headers=h).json()
    assert record["status"] == "registered" and record["high_risk"] is False
    # 产检血压 150/95 → 自动标高危
    visit = client.post(f"/api/maternal/records/{record['id']}/visits", json={"visit_type": "prenatal", "gest_week": 14, "bp": "150/95", "visit_date": "2026-08-05"}, headers=h).json()
    assert visit["high_risk"] is True
    # 未分娩不可结案
    assert client.post(f"/api/maternal/records/{record['id']}/close", headers=h).status_code == 409
    client.post(f"/api/maternal/records/{record['id']}/visits", json={"visit_type": "postpartum", "visit_date": "2027-02-10"}, headers=h)
    assert client.post(f"/api/maternal/records/{record['id']}/close", headers=h).json()["status"] == "closed"

    child = client.post("/api/maternal/children", json={"name": "宝宝", "gender": "男", "birth_date": "2027-02-06", "guardian_patient_id": base["patient"]["id"]}, headers=h).json()
    assert client.post(f"/api/maternal/children/{child['id']}/visits", json={"visit_type": "newborn", "weight_kg": 3.4, "visit_date": "2027-02-12"}, headers=h).status_code == 201


def test_25_vaccination_contraindication_block(client, h, base):
    pid = base["patient"]["id"]
    client.post("/api/vaccination/contraindications", json={"patient_id": pid, "vaccine_code": "FLU", "reason": "既往严重过敏反应"}, headers=h)
    check = client.get(f"/api/vaccination/pre-check?patient_id={pid}&vaccine_code=FLU", headers=h).json()
    assert check["allowed"] is False
    blocked = client.post("/api/vaccination/records", json={"patient_id": pid, "vaccine_code": "FLU", "vaccine_name": "流感疫苗", "org_id": base["township"]["id"]}, headers=h)
    assert blocked.status_code == 409
    ok = client.post("/api/vaccination/records", json={"patient_id": pid, "vaccine_code": "HEPB", "vaccine_name": "乙肝疫苗", "dose_no": 1, "org_id": base["township"]["id"], "vaccinated_date": "2026-08-10"}, headers=h)
    assert ok.status_code == 201
    check2 = client.get(f"/api/vaccination/pre-check?patient_id={pid}&vaccine_code=HEPB", headers=h).json()
    assert check2["allowed"] is True and check2["next_dose_no"] == 2


def test_26_27_28_publichealth(client, h, base):
    pid = base["patient"]["id"]
    event = client.post("/api/publichealth/events", json={"title": "流感聚集性疫情", "level": "III", "disease_name": "流行性感冒"}, headers=h).json()
    client.post(f"/api/publichealth/events/{event['id']}/actions", json={"action": "启动III级响应，学校晨检加密", "actor": "应急办"}, headers=h)
    assert len(client.get(f"/api/publichealth/events/{event['id']}/actions", headers=h).json()) == 1

    # ㉗ 诊间医防提醒：慢病超期 + 疫苗禁忌 + 活动事件
    chronic = client.post("/api/chronic", json={"patient_id": pid, "disease": "hypertension", "managed_by_org_id": base["township"]["id"]}, headers=h).json()
    client.post(f"/api/chronic/{chronic['id']}/followups", json={"sbp": 150, "dbp": 95, "next_due": "2026-01-01"}, headers=h)
    reminders = client.get(f"/api/publichealth/reminders/{pid}?today=2026-08-10", headers=h).json()["reminders"]
    types = {r["type"] for r in reminders}
    assert {"chronic_followup_overdue", "vaccine_contraindication", "active_ph_event", "lifestyle_guidance"} <= types

    client.post(f"/api/publichealth/events/{event['id']}/close", headers=h)
    assert client.post(f"/api/publichealth/events/{event['id']}/actions", json={"action": "late"}, headers=h).status_code == 409

    # ㉘ 其他卫生监测：超标自动标记
    monitor = client.post("/api/publichealth/monitors", json={"domain": "school", "org_id": base["township"]["id"], "indicator": "教室CO2浓度", "value": 2100, "threshold": 1500, "record_date": "2026-08-10"}, headers=h).json()
    assert monitor["exceeded"] is True
    assert len(client.get("/api/publichealth/monitors?exceeded=true", headers=h).json()) == 1


def test_30_hr_secondment(client, h, base):
    emp = client.post("/api/mgmt/employees", json={"org_id": base["county"]["id"], "name": "钱医生", "title": "副高", "position": "心内科医师"}, headers=h).json()
    sec = client.post("/api/mgmt/secondments", json={"employee_id": emp["id"], "to_org_id": base["township"]["id"], "start_date": "2026-08-01"}, headers=h).json()
    assert sec["status"] == "seconded"
    # 派驻中不可重复派驻
    assert client.post("/api/mgmt/secondments", json={"employee_id": emp["id"], "to_org_id": base["township"]["id"], "start_date": "2026-09-01"}, headers=h).status_code == 409
    assert client.get("/api/mgmt/secondments/stats", headers=h).json()["active_secondments"] == 1
    client.post(f"/api/mgmt/secondments/{sec['id']}/end?end_date=2027-02-01", headers=h)
    assert client.get("/api/mgmt/secondments/stats", headers=h).json()["active_secondments"] == 0


def test_31_finance_consolidation(client, h, base):
    for org, cat, amt in [(base["county"], "income", 500000), (base["county"], "expense", 420000), (base["township"], "income", 80000), (base["township"], "expense", 60000)]:
        client.post("/api/mgmt/finance", json={"org_id": org["id"], "period": "2026-07", "category": cat, "amount": amt}, headers=h)
    summary = client.get("/api/mgmt/finance/summary?period=2026-07", headers=h).json()
    assert summary["consolidated"] == {"income": 580000.0, "expense": 480000.0, "balance": 100000.0}
    township_row = next(o for o in summary["orgs"] if o["org_id"] == base["township"]["id"])
    assert township_row["balance"] == 20000.0


def test_32_assets(client, h, base):
    asset = client.post("/api/mgmt/assets", json={"org_id": base["county"]["id"], "code": "ZC-001", "name": "办公电脑", "category": "office", "quantity": 10}, headers=h).json()
    moved = client.post(f"/api/mgmt/assets/{asset['id']}/transfer?to_org_id={base['township']['id']}", headers=h).json()
    assert moved["org_id"] == base["township"]["id"]
    client.post(f"/api/mgmt/assets/{asset['id']}/scrap", headers=h)
    assert client.post(f"/api/mgmt/assets/{asset['id']}/transfer?to_org_id={base['county']['id']}", headers=h).status_code == 409


def test_34_official_docs(client, h):
    doc = client.post("/api/mgmt/docs", json={"title": "关于开展秋季传染病防控的通知", "doc_type": "notice", "issuer": "医共体办公室"}, headers=h).json()
    assert doc["status"] == "draft"
    published = client.post(f"/api/mgmt/docs/{doc['id']}/publish", headers=h).json()
    assert published["status"] == "published"
    assert client.post(f"/api/mgmt/docs/{doc['id']}/publish", headers=h).status_code == 409


def test_center_roster_qc_and_sample_flow(client, h, base):
    roster = client.post("/api/mgmt/rosters", json={"center_type": "imaging", "duty_date": "2026-08-11", "doctor_name": "影像科李医生"}, headers=h)
    assert roster.status_code == 201
    client.post("/api/mgmt/qc", json={"center_type": "lab", "item": "室内质控-血糖", "result": "fail", "note": "失控，重新定标", "record_date": "2026-08-10"}, headers=h).json()
    assert len(client.get("/api/mgmt/qc?result=fail", headers=h).json()) == 1

    lab = client.post("/api/exams", json={"patient_id": base["patient"]["id"], "from_org_id": base["township"]["id"], "center_type": "lab", "item_code": "GLU", "item_name": "空腹血糖"}, headers=h).json()
    statuses = [client.post(f"/api/exams/{lab['id']}/sample/advance", headers=h).json()["sample_status"] for _ in range(3)]
    assert statuses == ["collected", "in_transit", "received"]
    assert client.post(f"/api/exams/{lab['id']}/sample/advance", headers=h).status_code == 409
    # 影像申请无样本环节
    img = client.post("/api/exams", json={"patient_id": base["patient"]["id"], "from_org_id": base["township"]["id"], "center_type": "imaging", "item_code": "DR-1", "item_name": "胸部DR"}, headers=h).json()
    assert client.post(f"/api/exams/{img['id']}/sample/advance", headers=h).status_code == 422
