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

# ---------- 账号：各机构医师 + 一个居民端账户 ----------
# 必须放在业务动作之前——站内消息是投递给"已存在的收件人"的，
# 先出报告再建账号，那条危急值消息就没人收，演示站的消息中心会是空的。
for _u, _role, _org, _name in [
    ("doc_zhen1", "doctor", zhen1, "李镇医"),
    ("doc_village", "doctor", village, "赵村医"),
    ("doc_county", "doctor", county, "孙主任"),
    ("dir_demo", "director", county, "钱院长"),
]:
    c.post("/api/users", json={"username": _u, "password": "doctor123", "role": _role,
                               "org_id": _org["id"], "full_name": _name})

# ---------- 机构协作分组：两个片区 + 一个跨片区专科联盟 ----------
# 专科联盟刻意跨片区，好让"一家机构同时属于多个分组"在演示站上是可见的事实，
# 而不只是文档里的一句话。
if not c.get("/api/org-groups").json():
    _z1 = c.post("/api/org-groups", json={
        "name": "县医院片区", "group_type": "zone", "lead_org_id": county["id"],
        "note": "县人民医院牵头"}).json()
    _z2 = c.post("/api/org-groups", json={
        "name": "西部片区", "group_type": "zone", "lead_org_id": zhen2["id"],
        "note": "河西镇卫生院牵头"}).json()
    for _g, _members in [(_z1, [county, zhen1, village]), (_z2, [zhen2])]:
        for _m in _members:
            c.post(f"/api/org-groups/{_g['id']}/members", json={"org_id": _m["id"]})
    _al = c.post("/api/org-groups", json={
        "name": "胸痛专科联盟", "group_type": "alliance", "lead_org_id": county["id"]}).json()
    for _m in [county, village, zhen2]:
        c.post(f"/api/org-groups/{_al['id']}/members", json={"org_id": _m["id"]})

# 单独一个管理层会话：手术申请不得自批（职责分离），admin 建的单必须换个人审。
c_dir = httpx.Client(base_url=BASE, timeout=30)
c_dir.headers["Authorization"] = "Bearer " + c_dir.post(
    "/api/auth/login", json={"username": "dir_demo", "password": "doctor123"}
).json()["access_token"]

# 居民账户：手机号验证码登录 → 实名绑定到张伟，之后他的报告/手术/出院消息才有收件人
_resident_phone = "13800138001"
_code = c.post("/api/portal/auth/sms/code",
               json={"phone": _resident_phone, "purpose": "login"}).json().get("debug_code")
c_res = httpx.Client(base_url=BASE, timeout=30)
_resident_ready = bool(_code)
if _code:
    c_res.headers["Authorization"] = "Bearer " + c.post(
        "/api/portal/auth/sms/login",
        json={"phone": _resident_phone, "code": _code}).json()["access_token"]
    c_res.post("/api/portal/auth/realname",
               json={"name": "张伟", "id_card": "320981196503012345"})

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
# 抗菌药物使用强度：CEFUROXIME 维护了 DDD，AZITHROMYCIN 故意留空，
# 好让演示站上"未维护 DDD"的计数不是 0——这个数为 0 时没人知道它存在。
# ddd 与 daily_dose 同单位（mg）：头孢呋辛 DDD 为 3g，写 3 而不是 3000
# 会让使用强度差 1000 倍——接口对此有量纲合理性告警，但源头还是要填对。
c.post("/api/prescriptions/rules", json={"drug_code": "CEFUROXIME", "max_daily_dose": 4000,
                                         "dose_unit": "mg", "antibiotic": True, "ddd": 3000})
c.post("/api/prescriptions/rules", json={"drug_code": "AZITHROMYCIN", "max_daily_dose": 500,
                                         "dose_unit": "mg", "antibiotic": True, "ddd": 0})
for _code, _name, _dose in [("CEFUROXIME", "头孢呋辛", 3000), ("AZITHROMYCIN", "阿奇霉素", 500)]:
    c.post("/api/prescriptions", json={
        "patient_id": patients[2]["id"], "org_id": county["id"], "diagnosis_name": "社区获得性肺炎",
        "items": [{"drug_code": _code, "drug_name": _name, "daily_dose": _dose, "days": 7}]})
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

# ---------- 人员下沉调度：长期派驻满半年 + 一次巡诊（巡诊不计入下沉指标）----------
if not c.get("/api/staffing/secondments").json():
    _emps = c.get(f"/api/mgmt/employees?org_id={county['id']}").json()
    if not _emps:
        _emps = [c.post("/api/mgmt/employees", json={
            "org_id": county["id"], "name": n, "title": t, "position": "医师"}).json()
            for n, t in [("周主任", "主任医师"), ("吴主治", "主治医师"), ("郑医师", "医师")]]
    # 职称等级必须显式维护——平台不从"主任医师"这类文本推断等级
    for _e, _lv in zip(_emps[:3], ["senior", "intermediate", "none"]):
        if _lv != "none":
            # 是 PATCH 不是 POST——写错方法会静默 405，等级设不上，
            # 演示站的"中级及以上"列会永远是 0（这条自检就是为了盯住它）
            c.patch(f"/api/staffing/employees/{_e['id']}/title-level",
                    json={"title_level": _lv})
    _d200 = (date.today() - timedelta(days=200)).isoformat()
    _d30 = (date.today() - timedelta(days=30)).isoformat()
    c.post("/api/staffing/secondments", json={
        "employee_id": _emps[0]["id"], "from_org_id": county["id"], "to_org_id": zhen1["id"],
        "start_date": _d200, "assignment_type": "long_term", "position": "内科带教"})
    # 巡诊也满 200 天，但不该进"下沉满半年"的分子——演示站上这条对比很重要
    c.post("/api/staffing/secondments", json={
        "employee_id": _emps[1]["id"], "from_org_id": county["id"], "to_org_id": zhen2["id"],
        "start_date": _d200, "assignment_type": "rounds", "position": "每周巡诊"})
    if len(_emps) > 2:
        c.post("/api/staffing/secondments", json={
            "employee_id": _emps[2]["id"], "from_org_id": county["id"],
            "to_org_id": village["id"], "start_date": _d30, "assignment_type": "long_term"})

# ---------- 专病管理：一条四节点路径，一例在管一例出组 ----------
if not c.get("/api/disease-programs").json():
    _prog = c.post("/api/disease-programs", json={
        "code": "COPD", "name": "慢阻肺规范化诊疗", "org_id": county["id"],
        "description": "县域慢阻肺专病中心统一路径",
        "path_nodes": [
            {"key": "assess", "name": "首次评估", "required": True},
            {"key": "plan", "name": "制定治疗方案", "required": True},
            {"key": "review", "name": "3个月疗效复评", "required": True},
            {"key": "edu", "name": "戒烟与康复宣教", "required": False},
        ]}).json()
    _c_doc = httpx.Client(base_url=BASE, timeout=30)
    _c_doc.headers["Authorization"] = "Bearer " + c.post(
        "/api/auth/login", json={"username": "doc_county", "password": "doctor123"}
    ).json()["access_token"]
    # 一例走完全程并评价疗效
    _e1 = _c_doc.post(f"/api/disease-programs/{_prog['id']}/enrollments", json={
        "patient_id": patients[2]["id"], "org_id": county["id"]}).json()
    for _k, _r in [("assess", "CAT评分18分"), ("plan", "长效支气管扩张剂"), ("review", "CAT降至12分")]:
        _c_doc.post(f"/api/disease-programs/enrollments/{_e1['id']}/records",
                    json={"node_key": _k, "operator_name": "孙主任", "result": _r})
    _c_doc.post(f"/api/disease-programs/enrollments/{_e1['id']}/exit", json={
        "status": "completed", "outcome": "improved", "outcome_note": "症状明显缓解"})
    # 一例在管且必需节点未做完，演示站上"待办节点"才有内容。
    # 以镇卫生院医生的会话建（机构写入守卫：县医院医生不能替卫生院建档）
    _c_zhen = httpx.Client(base_url=BASE, timeout=30)
    _c_zhen.headers["Authorization"] = "Bearer " + c.post(
        "/api/auth/login", json={"username": "doc_zhen1", "password": "doctor123"}
    ).json()["access_token"]
    _e2 = _c_zhen.post(f"/api/disease-programs/{_prog['id']}/enrollments", json={
        "patient_id": patients[1]["id"], "org_id": zhen1["id"]}).json()
    _c_zhen.post(f"/api/disease-programs/enrollments/{_e2['id']}/records",
                 json={"node_key": "assess", "operator_name": "李镇医", "result": "CAT评分22分"})

# ---------- 医保基金总额付费：全域池走完 预付→预结→清算→分配 ----------
# 基金由管理层管（admin 会被 403 挡下），换 dir_demo 的会话。
if not c_dir.get("/api/fund/pools").json():
    _pool = c_dir.post("/api/fund/pools", json={
        "year": date.today().year, "insurance_type": "resident",
        "total_amount": 12000000, "prepay_ratio_pct": 70,
        "note": "全域城乡居民基金池（演示）"}).json()
    c_dir.post(f"/api/fund/pools/{_pool['id']}/prepayments", json={
        "batch_no": "YF-01", "amount": 8400000, "paid_date": date.today().isoformat(),
        "note": "年初预付 70%"})
    # 两期预结：一期人工核定、一期由结算单自动归集，让"来源"列有两种取值
    c_dir.post(f"/api/fund/pools/{_pool['id']}/periods", json={
        "period": f"{date.today().year}-01", "actual_amount": 5200000, "note": "一季度核定"})
    # 本月一期交给系统按结算单自动归集（period 变量在本行之后才定义，这里直接算）
    c_dir.post(f"/api/fund/pools/{_pool['id']}/periods",
               json={"period": date.today().strftime("%Y-%m")})
    # 清算按人工核定的全年发生额，好让演示站上有一笔确定的结余可分
    c_dir.post(f"/api/fund/pools/{_pool['id']}/settle", json={
        "total_expense": 11200000, "overrun_action": "none"})
    # 结余 80 万按绩效得分分配；得分快照在此刻冻结，之后调权不影响本次结果
    c_dir.post(f"/api/fund/pools/{_pool['id']}/distribute", json={"formula_expr": "score"})

# ---------- 门急诊文书：告知书模板 + 已签/拒签各一份 + 处置与护理记录 ----------
if not c.get("/api/outpatient/consent-templates").json():
    _tpl = c.post("/api/outpatient/consent-templates", json={
        "consent_type": "treatment", "title": "特殊治疗知情同意书", "version": "v1",
        "body": "本人已充分了解该项治疗的目的、方法、预期效果、可能的风险与并发症，"
                "以及可供选择的替代方案，自愿接受该项治疗。"}).json()
    c.post("/api/outpatient/consent-templates", json={
        "consent_type": "exam", "title": "增强CT检查知情同意书", "version": "v1",
        "body": "碘对比剂可能引起过敏反应，严重者可危及生命。本人无相关禁忌并自愿检查。"})

    # 告知书限医师开具——admin 会被 403 挡下，换县院医师的会话
    c_doc_c = httpx.Client(base_url=BASE, timeout=30)
    c_doc_c.headers["Authorization"] = "Bearer " + c.post(
        "/api/auth/login", json={"username": "doc_county", "password": "doctor123"}
    ).json()["access_token"]
    _enc_all = c.get(f"/api/encounters?patient_id={patients[0]['id']}").json()
    _enc_id = _enc_all[0]["id"] if _enc_all else 0

    _signed = c_doc_c.post("/api/outpatient/consents", json={
        "patient_id": patients[0]["id"], "org_id": county["id"], "consent_type": "treatment",
        "template_id": _tpl["id"], "related_type": "encounter", "related_id": _enc_id}).json()
    c_doc_c.post(f"/api/outpatient/consents/{_signed['id']}/sign",
                 json={"signer_name": "张伟", "signer_relation": "self"})
    # 拒签一份：拒签是一等状态，演示站上要看得见它长什么样
    _refused = c_doc_c.post("/api/outpatient/consents", json={
        "patient_id": patients[2]["id"], "org_id": county["id"], "consent_type": "surgery",
        "title": "手术知情同意书", "content": "手术可能出现出血、感染、麻醉意外等风险。"}).json()
    c_doc_c.post(f"/api/outpatient/consents/{_refused['id']}/refuse", json={
        "signer_name": "刘洋", "signer_relation": "self", "refuse_reason": "拟转上级医院手术"})

    if _enc_id:
        c_doc_c.post(f"/api/outpatient/encounters/{_enc_id}/treatments", json={
            "treatment_name": "雾化吸入", "site": "口鼻", "dose": "布地奈德混悬液 1mg",
            "executor_name": "王护士", "reaction": "无不适"})
        # 第二条故意不填反应：演示站上"未记录"的橙标要有实例
        c_doc_c.post(f"/api/outpatient/encounters/{_enc_id}/treatments", json={
            "treatment_name": "肌肉注射", "site": "臀部", "executor_name": "王护士"})
        c_doc_c.post(f"/api/outpatient/encounters/{_enc_id}/nursing-records", json={
            "content": "输液中，滴速40滴/分，患者无主诉不适", "nurse_name": "李护士"})

# ---------- 价格公示：一次带依据的调价，公示页才有"何时起执行"可看 ----------
_ct = _exists(c.get("/api/billing/charge-items").json(), lambda r: r["code"] == "EXAM-CT")
if _ct is None:
    _ct = c.post("/api/billing/charge-items", json={
        "code": "EXAM-CT", "name": "头颅CT平扫", "category": "exam", "price": 260}).json()
    c.post(f"/api/billing/charge-items/{_ct['id']}/reprice", json={
        "new_price": 220, "reason": "省医保局2026年第3号文（检查检验价格下调）",
        "effective_date": date.today().isoformat()})

# ---------- 就诊凭据：一张有效卡 + 一张挂失作废的旧卡 ----------
if not c.get(f"/api/credentials?patient_id={patients[0]['id']}").json():
    c.post("/api/credentials", json={"patient_id": patients[0]["id"], "credential_type": "card"})
    # 再发一张即自动作废上一张——挂失换发的正确语义，台账上两条都看得见
    c.post("/api/credentials", json={"patient_id": patients[0]["id"],
                                     "credential_type": "card", "reason": "原卡遗失挂失换发"})
    c.post("/api/credentials", json={"patient_id": patients[1]["id"],
                                     "credential_type": "qrcode"})

# 门诊药费明细：门诊药占比的数据源，不开明细这个指标恒为 0。
# 必须放在收费项目目录建好之后——计费明细要按 item_code 反查目录取价。
_enc = c.get(f"/api/encounters?patient_id={patients[2]['id']}").json()
if _enc and not c.get(f"/api/billing/details?encounter_id={_enc[0]['id']}").json():
    for _code, _qty in [("DRUG-ABX", 6), ("BED-DAY", 1)]:
        c.post("/api/billing/details", json={"patient_id": patients[2]["id"],
                                             "encounter_id": _enc[0]["id"],
                                             "item_code": _code, "quantity": _qty})


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
# 一例入出院诊断一致、一例不一致——演示站的"入出院诊断符合率"应当是 50%，
# 两例都改写诊断会让这个指标显示 0%，看起来像坏了。
full_inpatient_journey(patients[2], beds["01"], "社区获得性肺炎", "社区获得性肺炎", 3160, 1160)
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
    # 抢救转归：抢救成功率的唯一数据源。限医师判定，admin 会被 403 挡下，
    # 所以这里换 doc_county 的会话——这正是"职责分离"该有的样子。
    c_doc_county = httpx.Client(base_url=BASE, timeout=30)
    c_doc_county.headers["Authorization"] = "Bearer " + c.post(
        "/api/auth/login", json={"username": "doc_county", "password": "doctor123"}
    ).json()["access_token"]
    c_doc_county.post(f"/api/emergency/cases/{em['id']}/rescue-outcome",
                      json={"rescue_outcome": "success"})

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
    c_dir.post(f"/api/surgery/requests/{_req['id']}/approve", json={"approved": True})
    c.post(f"/api/surgery/requests/{_req['id']}/schedule", json={
        "room_id": room["id"], "scheduled_date": date.today().isoformat(),
        "start_time": "09:00", "end_time": "10:30"})
    c.post(f"/api/surgery/requests/{_req['id']}/record", json={
        "actual_surgery_name": "腹腔镜阑尾切除术", "anesthetist_name": "麻醉科周医生",
        "start_at": f"{date.today().isoformat()} 09:10", "end_at": f"{date.today().isoformat()} 10:05",
        "blood_loss_ml": 20, "findings": "阑尾化脓、周围少量脓苔", "outcome": "治愈",
        # 术前术后诊断符合率的数据源；括号内是分型补充，归一化后仍判为符合
        "preop_diagnosis": "急性阑尾炎", "postop_diagnosis": "急性阑尾炎（化脓性）"})
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

# ---------- 满意度：一条好评一条差评，管理端"差评清单"才有内容 ----------
if not c.get("/api/surveys?limit=5").json():
    for score, comment in [(5, "医生耐心，检查结果当天就出了"), (2, "上午挂号排队太久，希望增加号源")]:
        c.post("/api/surveys", json={"target_type": "encounter", "target_id": 1,
                                     "patient_id": patients[0]["id"], "score": score,
                                     "comment": comment})

print("演示数据灌入完成")
print(c.get("/api/metrics/overview").json())
print("DRG统计:", c.get("/api/drgs/stats").json())
print("就医流向:", c.get("/api/analytics/patient-flow").json())
print("科室成本:", [(x["dept_name"], x["total_cost"]) for x in
                 c.get(f"/api/cost/departments?period={period}&org_id={county['id']}").json()])
print("随访统计:", c.get("/api/followups/stats").json())
print("满意度:", c.get("/api/surveys/stats").json())
# 站内消息不单独灌数据——它必须由上面的报告出具/手术排班/出院动作派生出来。
# 收件人是"申请机构的医师"和"绑定档案的居民"，不是 admin——换他们的会话来看。
c_doc = httpx.Client(base_url=BASE, timeout=30)
c_doc.headers["Authorization"] = "Bearer " + c.post(
    "/api/auth/login", json={"username": "doc_village", "password": "doctor123"}
).json()["access_token"]
print("站内消息（村医未读）:", c_doc.get("/api/notifications/unread-count").json())
if _resident_ready:
    print("站内消息（居民未读）:", c_res.get("/api/portal/me/notifications/unread-count").json())
else:
    print("站内消息（居民）: 本次未取到验证码（60秒冷却），跳过居民侧核对")

# ---------- 全域慢专病：筛查→复核→分配→纳管→路径→监测→评估→转诊闭环→计分→日报 ----------
# 整块以"是否已有在管档案"做幂等开关：这条链路里几乎每一步都会派生任务与
# 消息，重复灌一遍会让演示站的待办翻倍，比不灌还难看。
if not c.get("/api/spd/enrollments?limit=1").json():
    _spd_team1 = c.post("/api/spd/teams", json={
        "name": "城东高血压管理团队", "org_id": zhen1["id"], "level": "township",
        "program_codes": ["hypertension"]}).json()
    _spd_team2 = c.post("/api/spd/teams", json={
        "name": "河西糖尿病管理团队", "org_id": zhen2["id"], "level": "township",
        "program_codes": ["diabetes"]}).json()

    # 村医赵村医进团队并领活：不这么做，医生移动端的慢专病工作台在演示环境上
    # 全是 0 与"未加入慢专病团队"——培训手册截出来的就是一张空表
    _vd_user = next(u for u in c.get("/api/users").json() if u["username"] == "doc_village")
    c.post(f"/api/spd/teams/{_spd_team1['id']}/members", json={
        "user_id": _vd_user["id"], "member_role": "village_doctor",
        "program_codes": ["hypertension"], "can_referral": True})
    c.post("/api/spd/village-doctors", json={
        "user_id": _vd_user["id"], "org_id": village["id"],
        "township": "城东镇", "village": "杨庄村", "phone": "13900001234"})

    _spd_patients = [
        c.post("/api/patients", json={
            "name": f"慢专病演示{i:02d}", "id_card": f"32098119601001{i:04d}",
            "gender": "女" if i % 2 else "男", "birth_date": "1960-10-01",
            "phone": f"1377100{i:04d}"}).json()
        for i in range(20)
    ]

    # 前 10 人走高血压筛查（量表高危答案），后 10 人走糖尿病筛查
    _high_hyp = {"family": "是", "salt": "是", "overweight": "是", "symptom": "是"}
    _high_dm = {"family": "是", "symptom": "是", "age": "是"}
    for i, p in enumerate(_spd_patients):
        hyper = i < 10
        c.post("/api/spd/screenings", json={
            "patient_id": p["id"], "program_code": "hypertension" if hyper else "diabetes",
            "source": "active", "org_id": (zhen1 if hyper else zhen2)["id"],
            "scale_code": "scr_hypertension" if hyper else "scr_diabetes",
            "answers": _high_hyp if hyper else _high_dm})

    # 高危复核确认 → 目标池 → 分配到团队
    for s in c.get("/api/spd/screenings?limit=100").json():
        if not s["reviewed"]:
            c.post(f"/api/spd/screenings/{s['id']}/review", json={"review_result": "confirmed"})
    for code, team in (("hypertension", _spd_team1), ("diabetes", _spd_team2)):
        _cands = [x["id"] for x in c.get(f"/api/spd/candidates?program_code={code}").json()
                  if x["status"] != "enrolled"]
        if _cands:
            c.post("/api/spd/candidates/distribute",
                   json={"candidate_ids": _cands, "team_id": team["id"]})

    # 签约纳管：风险等级错开，让工作台的分层统计不是一根柱子
    _risks = ["low", "mid", "high", "very_high"]
    _spd_enrolls = []
    for i, p in enumerate(_spd_patients):
        hyper = i < 10
        _enroll_body = {
            "patient_id": p["id"], "program_code": "hypertension" if hyper else "diabetes",
            "org_id": (zhen1 if hyper else zhen2)["id"],
            "team_id": (_spd_team1 if hyper else _spd_team2)["id"],
            "risk_level": _risks[i % 4], "consent_signed": True,
            "source": "screening"}
        if i < 6:  # 前 6 人归赵村医：签约积分与"我的患者"因此有数
            _enroll_body["village_doctor_id"] = _vd_user["id"]
            _enroll_body["doctor_user_id"] = _vd_user["id"]
        _spd_enrolls.append(c.post("/api/spd/enrollments", json=_enroll_body).json())

    # 标准路径：建一版演示模板并发布，前 5 个高血压患者入径、办结首节点
    _hyp_prog = next(pr for pr in c.get("/api/spd/programs").json()
                     if pr["code"] == "hypertension")
    _tpl = c.post("/api/spd/path-templates", json={
        "program_id": _hyp_prog["id"], "code": "demo_hyp_path",
        "name": "高血压演示路径", "scene": "followup"}).json()
    c.post(f"/api/spd/path-templates/{_tpl['id']}/nodes", json={
        "key": "assess", "name": "首次评估", "seq": 1, "next_key": "edu", "due_days": 7})
    c.post(f"/api/spd/path-templates/{_tpl['id']}/nodes", json={
        "key": "edu", "name": "生活方式宣教", "seq": 2, "service_type": "edu", "due_days": 14})
    c.post(f"/api/spd/path-templates/{_tpl['id']}/status", json={"status": "published"})
    for e in _spd_enrolls[:5]:
        c.post("/api/spd/path-instances",
               json={"enrollment_id": e["id"], "template_id": _tpl["id"]})
    for e in _spd_enrolls[:2]:  # 办结两份首节点任务，让路径推进与待办并存
        for t in c.get(f"/api/spd/tasks?patient_id={e['patient_id']}").json():
            if t["node_key"] == "assess" and t["status"] in ("pending", "claimed"):
                c.post(f"/api/spd/tasks/{t['id']}/complete",
                       json={"result": {"note": "演示办结：完成首次评估"}})

    # 村医的待办：两条随访 + 一条宣教，办结其中一条（随访积分随之入账）
    for _i, _e in enumerate(_spd_enrolls[:3]):
        _t = c.post("/api/spd/tasks", json={
            "patient_id": _e["patient_id"], "title": ["入户随访", "血压复测", "用药指导"][_i],
            "task_type": ["followup", "followup", "edu"][_i], "org_id": village["id"],
            "enrollment_id": _e["id"], "due_days": [0, 3, 7][_i]}).json()
        c.post(f"/api/spd/tasks/{_t['id']}/assign", json={"assignee_id": _vd_user["id"]})
        if _i == 2:
            c.post(f"/api/spd/tasks/{_t['id']}/complete",
                   json={"result": {"note": "已完成用药指导"}})

    # 监测数据：前几名血压异常，触发工作台异常提醒
    for i, e in enumerate(_spd_enrolls[:5]):
        c.post("/api/spd/measurements", json={
            "patient_id": e["patient_id"], "metric": "bp_sys", "value": 150 + i * 6,
            "unit": "mmHg", "program_code": "hypertension", "source": "manual"})

    # 综合风险评估：高危答案会自动派生干预与复诊（个案管理师端的联动）。
    # 干预模板按 auto_risk_level 匹配——不配模板就只会建复诊，演示不出联动
    c.post("/api/spd/intervention-templates", json={
        "code": "demo_hyp_itpl", "name": "极高危强化干预包", "program_code": "hypertension",
        "category": "drug", "content": "药物调整 + 每周家庭血压监测",
        "measures": "限盐、规律服药、每周2次随访电话", "frequency": "每周",
        "cycle_days": 30, "auto_risk_level": "very_high"})
    _high_answers = {"control": "未达标", "adherence": "差",
                     "complication": "2项及以上", "selfcare": "需协助"}
    for e in _spd_enrolls[:3]:
        c.post("/api/spd/assessments", json={
            "patient_id": e["patient_id"], "scale_code": "assess_risk_common",
            "program_code": "hypertension", "answers": _high_answers})

    # 逐级转诊闭环：发起→三级审核→到院→下转→随访接收
    _ref = c.post("/api/spd/referrals", json={
        "patient_id": _spd_enrolls[0]["patient_id"], "program_code": "hypertension",
        "direction": "up", "target_org_id": county["id"],
        "reason": "血压控制不佳，申请上级评估"}).json()
    for _ in range(3):
        c.post(f"/api/spd/referrals/{_ref['id']}/review",
               json={"action": "pass", "opinion": "同意上转"})
    c.post(f"/api/spd/referrals/{_ref['id']}/arrive",
           json={"effective_visit": True, "opinion": "已到院并完成专科评估"})
    c.post(f"/api/spd/referrals/{_ref['id']}/down",
           json={"target_org_id": zhen1["id"], "stable": True, "opinion": "病情稳定下转"})
    c.post(f"/api/spd/referrals/{_ref['id']}/receive-followup",
           json={"opinion": "已承接，纳入随访"})

    # 考核计分与日报：数字从上面这些动作里长出来，不是另灌的
    _spd_plan = c.post("/api/spd/assess-plans", json={
        "code": "demo_spd_plan", "name": "乡镇慢专病月度考核", "level": "township",
        "object_type": "org", "period_type": "month",
        "items": [{"indicator_code": "followup_rate", "weight": 50},
                  {"indicator_code": "referral_closure_rate", "weight": 50}]}).json()
    c.post("/api/spd/scores/run", json={
        "plan_id": _spd_plan["id"], "period": period,
        "object_ids": [zhen1["id"], zhen2["id"]]})
    c.post("/api/spd/report-templates", json={
        "code": "demo_spd_daily", "name": "慢专病运行日报", "period": "daily",
        "sections": [{"key": "summary", "title": "运行概况"},
                     {"key": "screening", "title": "筛查转化"},
                     {"key": "referral", "title": "转诊闭环"},
                     {"key": "todo", "title": "今日待办"}]})
    c.post("/api/spd/report-instances", json={"template_code": "demo_spd_daily"})

print("慢专病在管:", len(c.get("/api/spd/enrollments?limit=100").json()),
      "转诊闭环:", c.get("/api/spd/referrals-stats/closure").json().get("closed"))

# ---------- 末端自检 ----------
# 本脚本一路忽略响应码（很多步骤靠"已存在即跳过"保持幂等），代价是某一步
# 被新增的校验挡下来时会静悄悄地失败——手术自批禁令上线后，演示站的手术
# 页就空了一段时间，没人发现。以下断言只盯"走完流程"的终态，
# 让这类沉默失败在灌数据时当场暴露。
def _has_rows(resp):
    """真的返回了非空列表才算通过。

    错误响应体 `{"detail": "..."}` 也是真值——直接 `bool(resp.json())` 会让
    一次 401 冒充成功，自检形同虚设（这条是踩过的坑）。
    """
    data = resp.json() if resp.status_code == 200 else None
    return isinstance(data, list) and bool(data)


_checks = [
    ("手术已排班", lambda: bool(c.get("/api/surgery/schedules").json())),
    ("手术已出术中记录", lambda: any(
        r["status"] == "completed" for r in c.get("/api/surgery/requests").json())),
    ("危急值已产生", lambda: bool(c.get("/api/exams/critical").json())),
    ("住院已有出院病例", lambda: any(
        a["status"] == "discharged" for a in c.get("/api/inpatient/admissions").json())),
    ("危急值已落工作人员消息", lambda: _has_rows(c_doc.get("/api/notifications?limit=1"))),
    ("价格公示可免登录访问", lambda: _has_rows(
        httpx.Client(base_url=BASE, timeout=30).get("/api/portal/price-list"))),
    ("就诊凭据台账有作废记录", lambda: any(
        v["status"] == "void" for v in c.get("/api/credentials?limit=50").json())),
    # 分组统计的不变量：全部机构都入了片区，各片区之和才等于全域总数
    ("片区已覆盖全部机构", lambda: not c.get(
        "/api/org-groups/coverage?group_type=zone").json()["ungrouped"]),
    ("下沉指标中级及以上非零", lambda: any(
        o["long_term_6m_senior"] > 0
        for o in c.get("/api/staffing/dispatch-stats").json()["orgs"])),
    ("巡诊未被算成下沉", lambda: all(
        o["long_term_6m"] <= o["total"] for o in
        c.get("/api/staffing/dispatch-stats").json()["orgs"])),
    ("专病有在管病例且待办节点非空", lambda: any(
        e["status"] == "enrolled" and e["completion"]["pending_required"]
        for e in c.get("/api/disease-programs/enrollments?limit=50").json())),
    ("基金结余已完成分配", lambda: _has_rows(
        c_dir.get(f"/api/fund/pools/{c_dir.get('/api/fund/pools').json()[0]['id']}/distributions"))),
    ("知情告知书含拒签实例", lambda: any(
        x["status"] == "refused" for x in c.get("/api/outpatient/consents?limit=50").json())),
    ("手术质量指标已可算", lambda: any(
        i["key"] == "surgery_complication" and i["denominator"] > 0
        for i in c.get("/api/quality/clinical-indicators").json()["indicators"])),
    ("质量指标有分母", lambda: any(
        i["denominator"] > 0
        for i in c.get("/api/quality/clinical-indicators").json()["indicators"])),
    ("抗菌药物强度已可算", lambda: any(
        o["antibiotic_ddds"] > 0
        for o in c.get(f"/api/analytics/drug-use?period={period}").json()["orgs"])),
    # ---- 慢专病链路终态：筛出来的被管起来、转出去的收回来、数字算得出来
    ("慢专病筛查已转化为在管", lambda: any(
        e["status"] == "active" for e in c.get("/api/spd/enrollments?limit=100").json())),
    ("慢专病路径任务在办", lambda: _has_rows(c.get("/api/spd/tasks?open_only=true&limit=5"))),
    ("慢专病转诊已闭环", lambda: c.get(
        "/api/spd/referrals-stats/closure").json()["closed"] >= 1),
    ("高危评估已自动派生干预", lambda: _has_rows(c.get("/api/spd/interventions?limit=5"))),
    ("慢专病考核已出分", lambda: _has_rows(c.get("/api/spd/scores?limit=5"))),
    ("慢专病日报可出", lambda: _has_rows(c.get("/api/spd/report-instances?limit=5"))),
]
if _resident_ready:
    # 居民会话取决于短信验证码，60 秒冷却内重跑会拿不到；拿不到就别断言，
    # 否则本该幂等的脚本会因为"跑得太快"而报错。
    _checks.append(
        ("报告/手术已落居民消息",
         lambda: _has_rows(c_res.get("/api/portal/me/notifications?limit=1"))))

_failed = [name for name, check in _checks if not check()]
if _failed:
    raise SystemExit(f"演示数据自检未通过：{_failed}（多半是某个业务校验把中间步骤挡了）")
print("末端自检通过：", [name for name, _ in _checks])
