"""演示数据灌入脚本。

用法：先启动服务（uvicorn app.main:app --port 8000），再执行
    python scripts/seed_demo.py [base_url]
默认 base_url 为 http://127.0.0.1:8000。
"""
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
c = httpx.Client(base_url=BASE, timeout=30)
token = c.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
c.headers["Authorization"] = f"Bearer {token}"

county = c.post("/api/organizations", json={"name": "县人民医院", "org_type": "lead_hospital", "level": "county"}).json()
zhen1 = c.post("/api/organizations", json={"name": "城东镇卫生院", "org_type": "township", "level": "township", "parent_id": county["id"]}).json()
zhen2 = c.post("/api/organizations", json={"name": "河西镇卫生院", "org_type": "township", "level": "township", "parent_id": county["id"]}).json()
village = c.post("/api/organizations", json={"name": "杨庄村卫生室", "org_type": "village", "level": "village", "parent_id": zhen1["id"]}).json()

patients = [
    c.post("/api/patients", json={"name": name, "id_card": idc, "gender": g}).json()
    for name, idc, g in [
        ("张伟", "320981196503012345", "男"),
        ("王芳", "320981197807154321", "女"),
        ("刘洋", "320981199201017890", "男"),
    ]
]

c.post("/api/dictionaries/diagnosis/import", json=[
    {"code": "I10", "name": "特发性(原发性)高血压"},
    {"code": "E11", "name": "2型糖尿病"},
    {"code": "J44", "name": "慢性阻塞性肺疾病"},
    {"code": "J11", "name": "流行性感冒"},
])

for p, org, code, name in [
    (patients[0], village, "I10", "高血压"),
    (patients[1], zhen1, "E11", "2型糖尿病"),
    (patients[2], zhen2, "J44", "慢阻肺急性加重"),
    (patients[0], county, "I10", "高血压（复诊）"),
]:
    c.post("/api/encounters", json={"patient_id": p["id"], "org_id": org["id"], "doctor_name": "接诊医生",
                                    "diagnosis_code": code, "diagnosis_name": name, "summary": "常规诊疗记录"})

# 共享诊断中心：基层检查、上级诊断（含危急值与互认）
r1 = c.post("/api/exams", json={"patient_id": patients[0]["id"], "from_org_id": zhen1["id"], "center_type": "imaging",
                                "item_code": "DR-CHEST", "item_name": "胸部DR", "clinical_info": "咳嗽发热3天"}).json()
c.post(f"/api/exams/{r1['id']}/claim")
c.post(f"/api/exams/{r1['id']}/report", json={"finding": "右下肺斑片影", "conclusion": "考虑肺部感染", "critical": False, "reported_by": "县院影像科"})
r2 = c.post("/api/exams", json={"patient_id": patients[2]["id"], "from_org_id": village["id"], "center_type": "ecg",
                                "item_code": "ECG-12", "item_name": "十二导联心电图", "clinical_info": "胸闷心悸"}).json()
c.post(f"/api/exams/{r2['id']}/claim")
c.post(f"/api/exams/{r2['id']}/report", json={"finding": "ST段抬高", "conclusion": "急性心肌梗死可能，立即启动胸痛绿色通道", "critical": True, "reported_by": "县院心电中心"})
chk = c.get(f"/api/exams/recognition-check?patient_id={patients[0]['id']}&item_code=DR-CHEST").json()
c.post("/api/exams", json={"patient_id": patients[0]["id"], "from_org_id": county["id"], "center_type": "imaging",
                           "item_code": "DR-CHEST", "item_name": "胸部DR", "accept_recognition_of": chk["request_id"]})

# 审方与药房
c.post("/api/prescriptions/rules", json={"drug_code": "METFORMIN", "max_daily_dose": 2000, "dose_unit": "mg"})
c.post("/api/prescriptions", json={"patient_id": patients[1]["id"], "org_id": zhen1["id"], "diagnosis_name": "2型糖尿病",
                                   "items": [{"drug_code": "METFORMIN", "drug_name": "二甲双胍", "daily_dose": 1500, "days": 30}]})
c.post("/api/prescriptions", json={"patient_id": patients[1]["id"], "org_id": village["id"], "diagnosis_name": "2型糖尿病",
                                   "items": [{"drug_code": "METFORMIN", "drug_name": "二甲双胍", "daily_dose": 2500, "days": 30}]})
c.post("/api/pharmacy/stocks", json={"org_id": county["id"], "drug_code": "METFORMIN", "drug_name": "二甲双胍", "quantity": 500, "threshold": 100})
c.post("/api/pharmacy/stocks", json={"org_id": zhen1["id"], "drug_code": "METFORMIN", "drug_name": "二甲双胍", "quantity": 30, "threshold": 50})
c.post("/api/pharmacy/transfers", json={"drug_code": "METFORMIN", "from_org_id": county["id"], "to_org_id": zhen1["id"], "quantity": 100})

# 慢病、转诊、传染病
ch1 = c.post("/api/chronic", json={"patient_id": patients[0]["id"], "disease": "hypertension", "managed_by_org_id": zhen1["id"]}).json()
c.post(f"/api/chronic/{ch1['id']}/followups", json={"sbp": 165, "dbp": 102, "next_due": "2026-07-01"})
ch2 = c.post("/api/chronic", json={"patient_id": patients[1]["id"], "disease": "diabetes", "managed_by_org_id": zhen1["id"]}).json()
c.post(f"/api/chronic/{ch2['id']}/followups", json={"glucose": 7.8, "next_due": "2026-09-15"})
ref = c.post("/api/referrals", json={"patient_id": patients[0]["id"], "from_org_id": zhen1["id"], "to_org_id": county["id"],
                                     "direction": "up", "reason": "血压3级，建议上级调整方案"}).json()
c.patch(f"/api/referrals/{ref['id']}/status", json={"status": "accepted"})
c.patch(f"/api/referrals/{ref['id']}/status", json={"status": "completed"})
for i, org in enumerate([zhen1, zhen1, zhen2, village, county]):
    c.post("/api/infectious/cases", json={"org_id": org["id"], "disease_code": "J11", "disease_name": "流行性感冒",
                                          "onset_date": f"2026-08-0{i + 4}"})

# 远程会诊
cons = c.post("/api/consultations", json={"patient_id": patients[2]["id"], "from_org_id": village["id"],
                                          "to_org_id": county["id"], "question": "心电图ST段抬高，请求心内科急会诊"}).json()
c.post(f"/api/consultations/{cons['id']}/accept", json={"expert_name": "心内科张主任"})
c.post(f"/api/consultations/{cons['id']}/complete", json={"opinion": "确认急性心梗，立即转入导管室行PCI"})
c.post(f"/api/consultations/{cons['id']}/rate", json={"rating": 5})

# 家医签约与履约
ct = c.post("/api/contracts", json={"patient_id": patients[0]["id"], "org_id": zhen1["id"],
                                    "doctor_name": "李家医", "package": "standard", "signed_date": "2026-08-01"}).json()
c.post(f"/api/contracts/{ct['id']}/services", json={"service_type": "visit", "note": "上门测血压"})
c.post(f"/api/contracts/{ct['id']}/services", json={"service_type": "followup", "note": "季度随访"})

# 预约诊疗
slot = c.post("/api/appointments/slots", json={"org_id": county["id"], "resource_type": "exam",
                                               "resource_name": "CT室上午", "slot_date": "2026-08-20",
                                               "slot_time": "09:00-10:00", "capacity": 5}).json()
c.post("/api/appointments", json={"slot_id": slot["id"], "patient_id": patients[0]["id"]})

# 消毒供应与医废
batch = c.post("/api/cssd/batches", json={"batch_no": "CSSD-20260810-01", "center_org_id": county["id"],
                                          "item_name": "手术器械包", "quantity": 20}).json()
c.post(f"/api/cssd/batches/{batch['id']}/advance")
c.post(f"/api/cssd/batches/{batch['id']}/advance?dispatched_to_org_id=" + str(zhen1["id"]))
c.post("/api/medwaste", json={"org_id": zhen1["id"], "waste_type": "infectious", "weight_kg": 3.5, "collected_date": "2026-08-09"})
c.post("/api/medwaste", json={"org_id": zhen2["id"], "waste_type": "sharp", "weight_kg": 1.2, "collected_date": "2026-08-01"})

print("演示数据灌入完成")
print(c.get("/api/metrics/overview").json())
