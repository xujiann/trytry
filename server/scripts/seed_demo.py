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

def ensure_org(payload):
    """机构幂等创建：已存在（409）则从列表反查返回既有记录，支持脚本重复执行。"""
    resp = c.post("/api/organizations", json=payload)
    if resp.status_code == 201:
        return resp.json()
    return next(o for o in c.get("/api/organizations").json() if o["name"] == payload["name"])


county = ensure_org({"name": "县人民医院", "org_type": "lead_hospital", "level": "county"})
zhen1 = ensure_org({"name": "城东镇卫生院", "org_type": "township", "level": "township", "parent_id": county["id"]})
zhen2 = ensure_org({"name": "河西镇卫生院", "org_type": "township", "level": "township", "parent_id": county["id"]})
village = ensure_org({"name": "杨庄村卫生室", "org_type": "village", "level": "village", "parent_id": zhen1["id"]})

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

# 家医签约与履约（幂等：已有有效签约时跳过）
ct_resp = c.post("/api/contracts", json={"patient_id": patients[0]["id"], "org_id": zhen1["id"],
                                         "doctor_name": "李家医", "package": "standard", "signed_date": "2026-08-01"})
if ct_resp.status_code == 201:
    ct = ct_resp.json()
    c.post(f"/api/contracts/{ct['id']}/services", json={"service_type": "visit", "note": "上门测血压"})
    c.post(f"/api/contracts/{ct['id']}/services", json={"service_type": "followup", "note": "季度随访"})

# 预约诊疗
slot = c.post("/api/appointments/slots", json={"org_id": county["id"], "resource_type": "exam",
                                               "resource_name": "CT室上午", "slot_date": "2026-08-20",
                                               "slot_time": "09:00-10:00", "capacity": 5}).json()
c.post("/api/appointments", json={"slot_id": slot["id"], "patient_id": patients[0]["id"]})

# 消毒供应与医废（消毒批次幂等：同批号已存在时跳过）
batch_resp = c.post("/api/cssd/batches", json={"batch_no": "CSSD-20260810-01", "center_org_id": county["id"],
                                               "item_name": "手术器械包", "quantity": 20})
if batch_resp.status_code == 201:
    batch = batch_resp.json()
    c.post(f"/api/cssd/batches/{batch['id']}/advance")
    c.post(f"/api/cssd/batches/{batch['id']}/advance?dispatched_to_org_id=" + str(zhen1["id"]))
c.post("/api/medwaste", json={"org_id": zhen1["id"], "waste_type": "infectious", "weight_kg": 3.5, "collected_date": "2026-08-09"})
c.post("/api/medwaste", json={"org_id": zhen2["id"], "waste_type": "sharp", "weight_kg": 1.2, "collected_date": "2026-08-01"})

# ================= 第四阶段块6：新模块演示数据（幂等：存在即跳过） =================
from datetime import date, timedelta


def _exists(rows, pred):
    return next((r for r in rows if pred(r)), None)


# ---------- 互认目录（2 项） ----------
_items = c.get("/api/exams/recognition-items").json()
for payload in [
    {"item_code": "DR-CHEST", "item_name": "胸部DR", "center_type": "imaging", "mutual_scope": "county"},
    {"item_code": "CT-HEAD", "item_name": "头颅CT平扫", "center_type": "imaging", "mutual_scope": "city"},
]:
    if not _exists(_items, lambda r, p=payload: r["item_code"] == p["item_code"]):
        c.post("/api/exams/recognition-items", json=payload)

# ---------- 住院：病区/床位 + 两次完整入出院（含医嘱/病案首页→DRG入组/结算） ----------
_wards = c.get(f"/api/inpatient/wards?org_id={county['id']}").json()
ward = _exists(_wards, lambda w: w["name"] == "内科一病区") or c.post(
    "/api/inpatient/wards", json={"org_id": county["id"], "name": "内科一病区"}
).json()
_beds = c.get(f"/api/inpatient/beds?ward_id={ward['id']}").json()
beds = {}
for bed_no in ("01", "02"):
    beds[bed_no] = _exists(_beds, lambda b, n=bed_no: b["bed_no"] == n) or c.post(
        "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": bed_no}
    ).json()

# 收费项目（结算用）
_charges = c.get("/api/billing/charge-items").json()
for payload in [
    {"code": "BED-DAY", "name": "普通床位费/日", "category": "bed", "price": 40},
    {"code": "DRUG-ABX", "name": "注射用抗菌药物", "category": "drug", "price": 58},
]:
    if not _exists(_charges, lambda r, p=payload: r["code"] == p["code"]):
        c.post("/api/billing/charge-items", json=payload)


def full_inpatient_journey(patient, bed, diagnosis, discharge_diagnosis, total_cost, drug_cost):
    """一次完整住院旅程：入院→医嘱→计费→病案首页(自动DRG入组)→结算→出院。幂等：该患者有住院史即跳过。"""
    if c.get(f"/api/inpatient/admissions?patient_id={patient['id']}").json():
        return
    adm = c.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"],
              "doctor_name": "内科王医生", "diagnosis_name": diagnosis},
    ).json()
    c.post("/api/inpatient/orders", json={"admission_id": adm["id"], "order_type": "long", "content": "一级护理，抗感染治疗"})
    c.post("/api/billing/details", json={"patient_id": patient["id"], "admission_id": adm["id"], "item_code": "BED-DAY", "quantity": 5})
    c.post("/api/billing/details", json={"patient_id": patient["id"], "admission_id": adm["id"], "item_code": "DRUG-ABX", "quantity": 2})
    # 病案首页：出院诊断命中 DRG 分组目录关键词，自动入组（drg_code/drg_weight 回填）
    c.post(
        f"/api/inpatient/admissions/{adm['id']}/case-summary",
        json={"discharge_diagnosis": discharge_diagnosis, "total_cost": total_cost,
              "drug_cost": drug_cost, "outcome": "治愈"},
    )
    # 费用结清后方可出院
    c.post("/api/billing/settlements", json={"bill_type": "inpatient", "admission_id": adm["id"], "insurance_pay": 200})
    c.post(f"/api/inpatient/admissions/{adm['id']}/discharge")


# DRG 入组示例：肺炎（ES31，权重0.95）与心肌梗死（FM19，权重1.42），CMI 可对比
full_inpatient_journey(patients[2], beds["01"], "社区获得性肺炎", "肺炎", 3160, 1160)
full_inpatient_journey(patients[1], beds["02"], "急性心肌梗死", "心肌梗死（PCI术后）", 31600, 5200)

# ---------- 不良事件闭环：上报→管理层审核→整改 ----------
_events = c.get("/api/quality/adverse-events").json()
if not _exists(_events, lambda e: e["event_type"] == "medication"):
    ev = c.post(
        "/api/quality/adverse-events",
        json={"org_id": zhen1["id"], "event_type": "medication", "level": "III",
              "description": "口服药发放剂量与医嘱不符，及时发现未造成伤害"},
    ).json()
    c.post(f"/api/quality/adverse-events/{ev['id']}/review", json={"note": "属实，纳入月度质量分析"})
    c.post(f"/api/quality/adverse-events/{ev['id']}/rectify", json={"note": "发药双人核对制度已落实"})

# ---------- 传染病目录场景：甲类（霍乱2小时时限）跨日补报 → 迟报清单 ----------
if not c.get("/api/infectious/cases?disease_code=A00").json():
    c.post(
        "/api/infectious/cases",
        json={"org_id": county["id"], "disease_code": "A00", "disease_name": "霍乱",
              "onset_date": (date.today() - timedelta(days=3)).isoformat()},
    )

# ---------- 急救绿道：胸痛通道 + 完整时间轴 ----------
_em_cases = c.get("/api/emergency/cases").json()
if not _exists(_em_cases, lambda e: e["channel_type"] == "chest_pain" and e["location"] == "城东镇农贸市场"):
    em = c.post(
        "/api/emergency/cases",
        json={"caller_phone": "13900001234", "location": "城东镇农贸市场",
              "symptom": "胸痛大汗30分钟", "ambulance_no": "苏A120-01",
              "dest_org_id": county["id"], "channel_type": "chest_pain"},
    ).json()
    d = date.today().isoformat()
    for milestone, at in [
        ("onset", f"{d} 09:10"), ("call", f"{d} 09:22"), ("depart", f"{d} 09:25"),
        ("arrive_scene", f"{d} 09:38"), ("arrive_hospital", f"{d} 09:58"), ("treatment", f"{d} 10:05"),
    ]:
        c.post(f"/api/emergency/cases/{em['id']}/milestones", json={"milestone": milestone, "occurred_at": at})
    for _ in range(3):  # 出车→到场→到院
        c.post(f"/api/emergency/cases/{em['id']}/advance")
    c.post(f"/api/emergency/cases/{em['id']}/vitals", json={"heart_rate": 110, "sbp": 90, "dbp": 60, "spo2": 93, "note": "车载心电已回传"})

print("演示数据灌入完成")
print(c.get("/api/metrics/overview").json())
print("DRG统计:", c.get("/api/drgs/stats").json())
