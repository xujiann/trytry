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

# ================= 阶段一~五：新模块演示数据（T6.5，幂等：存在即跳过） =================
period = date.today().strftime("%Y-%m")

# ---------- 住院临床文书：给在院患者补齐病程/护理/体温单 ----------
_in_hospital = [a for a in c.get("/api/inpatient/admissions").json() if a["status"] == "admitted"]
if not _in_hospital:
    # 前面的住院旅程都已出院，这里再收一位在院患者，好让文书与手术页有数据
    _bed = c.post("/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "03"}).json()
    _adm = c.post(
        "/api/inpatient/admissions",
        json={"patient_id": patients[0]["id"], "ward_id": ward["id"], "bed_id": _bed["id"],
              "doctor_name": "外科李医生", "diagnosis_name": "急性阑尾炎"},
    ).json()
    _in_hospital = [_adm]
adm_id = _in_hospital[0]["id"]

if not c.get(f"/api/inpatient/admissions/{adm_id}/progress-notes").json():
    today = date.today().isoformat()
    for note_type, content, at in [
        ("first", "患者因转移性右下腹痛12小时入院，查体麦氏点压痛反跳痛阳性，拟行阑尾切除术。", f"{today} 08:30"),
        ("daily", "术后第1天，体温37.4℃，切口无渗出，肠鸣音恢复，继续抗感染。", f"{today} 09:00"),
        ("ward_round", "主任查房：恢复顺利，明日可进流质，注意切口换药。", f"{today} 10:15"),
    ]:
        c.post(f"/api/inpatient/admissions/{adm_id}/progress-notes",
               json={"note_type": note_type, "content": content,
                     "doctor_name": "外科李医生", "recorded_at": at})
    for level, content, at in [
        ("level1", "一级护理，持续心电监护，观察切口渗血。", f"{today} 08:40"),
        ("level2", "改二级护理，协助下床活动。", f"{today} 14:00"),
    ]:
        c.post(f"/api/inpatient/admissions/{adm_id}/nursing-records",
               json={"nursing_level": level, "content": content, "nurse_name": "王护士", "recorded_at": at})
    for at, temp, pulse, resp_rate, sbp, dbp in [
        (f"{today} 06:00", 38.2, 96, 20, 118, 76),
        (f"{today} 10:00", 37.6, 88, 19, 120, 78),
        (f"{today} 14:00", 37.1, 82, 18, 116, 74),
        (f"{today} 18:00", 36.8, 78, 18, 118, 75),
    ]:
        c.post(f"/api/inpatient/admissions/{adm_id}/vitals",
               json={"measured_at": at, "temperature": temp, "pulse": pulse,
                     "respiration": resp_rate, "sbp": sbp, "dbp": dbp, "recorder": "王护士"})
    c.post("/api/inpatient/handovers",
           json={"ward_id": ward["id"], "shift": "night", "handover_date": today,
                 "from_staff": "白班王护士", "to_staff": "夜班赵护士", "critical_count": 0,
                 "content": "3床术后第1天，注意切口渗血与体温变化。"})

# ---------- 手术麻醉：手术间 + 一台走完全流程的手术 ----------
_rooms = c.get(f"/api/surgery/rooms?org_id={county['id']}").json()
room = _rooms[0] if _rooms else c.post(
    "/api/surgery/rooms", json={"org_id": county["id"], "name": "一号手术间"}).json()
if not c.get(f"/api/surgery/requests?admission_id={adm_id}").json():
    _req = c.post("/api/surgery/requests", json={
        "admission_id": adm_id, "surgery_name": "腹腔镜阑尾切除术", "incision_level": "II",
        "anesthesia_type": "general", "urgency": "urgent", "surgeon_name": "外科李医生",
        "planned_date": date.today().isoformat()}).json()
    c.post(f"/api/surgery/requests/{_req['id']}/approve", json={"approved": True})
    c.post(f"/api/surgery/requests/{_req['id']}/schedule", json={
        "room_id": room["id"], "scheduled_date": date.today().isoformat(),
        "start_time": "09:00", "end_time": "10:30"})
    c.post(f"/api/surgery/requests/{_req['id']}/record", json={
        "actual_surgery_name": "腹腔镜阑尾切除术", "anesthetist_name": "麻醉科周医生",
        "start_at": f"{date.today().isoformat()} 09:10", "end_at": f"{date.today().isoformat()} 10:05",
        "blood_loss_ml": 20, "findings": "阑尾化脓、周围少量脓苔", "outcome": "治愈"})
    # 高值耗材绑定到这台手术，构成可追溯链
    c.post("/api/materials/consumables", json={
        "barcode": "HV-DEMO-0001", "name": "一次性腹腔镜穿刺器", "spec": "10mm",
        "org_id": county["id"], "batch_no": "B2026DEMO", "expire_date": "2028-06-30",
        "unit_price": 480})
    c.post("/api/materials/consumables/HV-DEMO-0001/use", json={
        "patient_id": patients[0]["id"], "surgery_id": _req["id"]})

# ---------- 随访中心：术后与出院随访已自动派生，这里补一条慢病随访 ----------
if not c.get("/api/followups?category=chronic").json():
    c.post("/api/followups", json={
        "patient_id": patients[1]["id"], "org_id": zhen1["id"], "category": "chronic",
        "title": "糖尿病季度随访", "due_date": (date.today() - timedelta(days=2)).isoformat(),
        "assigned_to": "李家医"})

# ---------- 会计核算：两张凭证，一张过账一张留草稿 ----------
if not c.get(f"/api/accounting/vouchers?period={period}").json():
    v1 = c.post("/api/accounting/vouchers", json={
        "org_id": county["id"], "voucher_no": "JZ-2026-001", "voucher_date": f"{period}-05",
        "summary": "收取门诊医疗款",
        "entries": [{"subject_code": "1002", "summary": "存入银行", "debit": 128600},
                    {"subject_code": "4001", "summary": "医疗收入", "credit": 128600}]}).json()
    c.post(f"/api/accounting/vouchers/{v1['id']}/post")
    c.post("/api/accounting/vouchers", json={
        "org_id": county["id"], "voucher_no": "JZ-2026-002", "voucher_date": f"{period}-08",
        "summary": "计提当月人员经费（待复核）",
        "entries": [{"subject_code": "5001", "summary": "医疗业务成本", "debit": 86000},
                    {"subject_code": "2201", "summary": "应付职工薪酬", "credit": 86000}]})

# ---------- 成本核算：科室、直接成本与分摊规则 ----------
_depts = {d["code"]: d for d in c.get(f"/api/mgmt/departments?org_id={county['id']}").json()}
for code, name, category in [("NK", "内科", "clinical"), ("WK", "外科", "clinical"),
                             ("YJ", "医技科", "medtech"), ("XZ", "行政后勤", "admin")]:
    if code not in _depts:
        _depts[code] = c.post("/api/mgmt/departments", json={
            "org_id": county["id"], "code": code, "name": name, "category": category}).json()
if not c.get(f"/api/cost/departments?period={period}&org_id={county['id']}").json():
    for code, cost_type, amount in [
        ("NK", "labor", 320000), ("NK", "drug", 180000), ("NK", "consumable", 46000),
        ("WK", "labor", 285000), ("WK", "consumable", 132000), ("WK", "depreciation", 38000),
        ("YJ", "labor", 96000), ("YJ", "depreciation", 54000),
        ("XZ", "labor", 78000), ("XZ", "overhead", 42000),
    ]:
        c.post("/api/cost/departments", json={
            "dept_id": _depts[code]["id"], "period": period, "cost_type": cost_type, "amount": amount})
    for source, target, ratio in [("XZ", "NK", 55), ("XZ", "WK", 45), ("YJ", "NK", 60), ("YJ", "WK", 40)]:
        c.post("/api/cost/allocation-rules", json={
            "from_dept_id": _depts[source]["id"], "to_dept_id": _depts[target]["id"], "ratio_pct": ratio})

# ---------- 物资采购：一单走到验收 ----------
if not c.get("/api/materials/purchases").json():
    _supplier = next((s for s in c.get("/api/pharmacy/suppliers").json() if s["name"] == "康泰医疗器械"),
                     None) or c.post("/api/pharmacy/suppliers",
                                     json={"name": "康泰医疗器械", "contact": "刘经理"}).json()
    _mp = c.post("/api/materials/purchases", json={
        "org_id": county["id"], "dept_id": _depts["NK"]["id"], "item_name": "移动输液架",
        "spec": "不锈钢五轮", "unit": "个", "quantity": 30, "estimated_price": 185,
        "reason": "病区更新"}).json()
    c.post(f"/api/materials/purchases/{_mp['id']}/approve", json={"approved": True})
    c.post(f"/api/materials/purchases/{_mp['id']}/contract", json={
        "supplier_id": _supplier["id"], "contract_no": "HT-2026-018", "contract_amount": 5550})
    c.post(f"/api/materials/purchases/{_mp['id']}/receive", json={
        "received_quantity": 30, "note": "外观完好，数量相符"})

# ---------- 决策指标：县外就诊（有序与自行外出各一）+ 两条绩效公式 ----------
if not c.get("/api/analytics/outbound-visits").json():
    # 转出与转入必须是不同机构（接口有校验）：乡镇 → 县级，再由县级转出县域
    _out_ref = c.post("/api/referrals", json={
        "patient_id": patients[2]["id"], "from_org_id": zhen2["id"], "to_org_id": county["id"],
        "direction": "up", "reason": "需上级医院进一步诊治"})
    c.post("/api/analytics/outbound-visits", json={
        "patient_id": patients[2]["id"], "visit_date": date.today().isoformat(),
        "external_org_name": "市第一人民医院", "external_org_level": "city",
        "visit_type": "inpatient", "total_amount": 28600, "insurance_pay": 19800,
        "referral_id": _out_ref.json()["id"] if _out_ref.status_code == 201 else None})
    c.post("/api/analytics/outbound-visits", json={
        "patient_id": patients[1]["id"], "visit_date": date.today().isoformat(),
        "external_org_name": "省人民医院", "external_org_level": "province",
        "visit_type": "outpatient", "total_amount": 1260, "insurance_pay": 0})
if not c.get("/api/analytics/formulas").json():
    c.post("/api/analytics/formulas", json={
        "key": "up_referral_rate", "name": "上转占比", "unit": "%",
        "expression": "round(referrals_up / encounters * 100, 2)", "weight": 40})
    c.post("/api/analytics/formulas", json={
        "key": "bed_efficiency", "name": "床位使用率", "unit": "%",
        "expression": "bed_occupancy_rate_pct", "weight": 60})

# ---------- 规则引擎：两条统一规则 ----------
if not c.get("/api/rules").json():
    c.post("/api/rules", json={
        "key": "rx_over_max_dose", "name": "超最大日剂量", "domain": "prescription",
        "condition": "daily_dose > max_daily_dose", "message": "日剂量超过说明书上限，转药师审",
        "severity": "error", "deduct_points": 10})
    c.post("/api/rules", json={
        "key": "mr_chief_too_short", "name": "主诉过于简略", "domain": "medical_record",
        "condition": "len(chief_complaint) < 6", "message": "主诉少于6字，补充症状与时长",
        "severity": "warning", "deduct_points": 5})

# ---------- 流程引擎：新药引进审批走到药学审核节点 ----------
if not c.get("/api/workflows/definitions").json():
    c.post("/api/workflows/definitions", json={
        "key": "drug_intro", "name": "新药引进审批",
        "nodes": [{"key": "apply", "name": "科室申请", "role": "doctor", "next": "pharmacy"},
                  {"key": "pharmacy", "name": "药学审核", "role": "pharmacist", "next": "approve"},
                  {"key": "approve", "name": "院长审批", "role": "director", "next": ""}]})
    _inst = c.post("/api/workflows/instances", json={
        "definition_key": "drug_intro", "business_type": "drug_intro", "business_id": 1,
        "title": "引进某长效降压药", "org_id": county["id"]}).json()
    c.post(f"/api/workflows/instances/{_inst['id']}/advance", json={"comment": "内科提出，临床确有需求"})

print("演示数据灌入完成")
print(c.get("/api/metrics/overview").json())
print("DRG统计:", c.get("/api/drgs/stats").json())
print("就医流向:", c.get("/api/analytics/patient-flow").json())
print("科室成本:", [(x["dept_name"], x["total_cost"]) for x in
                 c.get(f"/api/cost/departments?period={period}&org_id={county['id']}").json()])
print("随访统计:", c.get("/api/followups/stats").json())
