"""病历质控 MRQC12 的「最近一次出院」按出院时刻取，不按编号（P2-877，P2-870「不需要拍板的一半」；第二十三批「"最新 / 最近一次"
取的是哪一条」扫描 Y2-3）。

`quality._record_context` 取该患者编号最大的已出院住院、查那次住院的病案首页，MRQC12「出院须有病案首页」据此扣 12 分。补导的
历史住院编号与入院时间无关（ADR-0018）：2026 年在平台住院、写了首页、正常出院的患者，之后补导一条 2019 年的出院（编号更大、
没有首页），此后写的病历就按那条 2019 年的住院扣 MRQC12，原来 100 分的病历复评成 88。修后按出院时刻（没有出院时刻的按入院）
倒序、编号兜底，与出院随访匹配（P2-692）同一句。门诊病历参不参与、HIS 同步 / 只有导入出院的怎么算随 P2-870 待裁定。
"""
from datetime import datetime

from app.database import SessionLocal
from app.models import Admission, User

GOOD = dict(chief_complaint="头痛3天",
            present_illness="患者3天前无明显诱因出现头痛，呈持续性胀痛，伴头晕，无恶心呕吐，无发热，自测血压偏高，休息后不缓解，遂来就诊。",
            past_history="高血压病史5年", physical_exam="体温36.5℃，脉搏78次/分，呼吸18次/分，血压150/95mmHg",
            diagnosis_basis="头痛3天，既往高血压病史，查体血压150/95mmHg，结合症状考虑高血压病2级",
            treatment_plan="调整降压药物，低盐饮食，监测血压，一周后复诊")


def _record_defects(client, admin, org, patient):
    encounter = client.post("/api/encounters", headers=admin, json={
        "patient_id": patient, "org_id": org, "doctor_name": "门诊医生", "encounter_type": "outpatient",
        "diagnosis_name": "高血压"})
    assert encounter.status_code == 201, encounter.text
    made = client.post("/api/quality/records", headers=admin, json={"encounter_id": encounter.json()["id"], **GOOD})
    assert made.status_code == 201, made.text
    return [d["rule_code"] for d in made.json()["qc"]["defects"]]


def test_平台出院有首页_补导一条更早的出院_不按那条扣MRQC12(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2877 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2877 患者", "id_card": "330127196101012877"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2877 内科"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P2877-1"}).json()["id"]
    admitted = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "doctor_name": "县医生", "diagnosis_name": "肺炎"})
    assert admitted.status_code == 201, admitted.text
    admission = admitted.json()["id"]
    summary = client.post(f"/api/inpatient/admissions/{admission}/case-summary", headers=admin,
                          json={"discharge_diagnosis": "肺炎"})
    assert summary.status_code == 201, summary.text
    assert client.post(f"/api/inpatient/admissions/{admission}/discharge", headers=admin).status_code == 200
    assert "MRQC12" not in _record_defects(client, admin, org, patient)   # 平台出院有首页：不扣

    with SessionLocal() as db:   # 上线后补导的 2019 年住院：编号更大、没有首页（`import_legacy` 不建首页）
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        db.add(Admission(patient_id=patient, org_id=org, ward_id=ward, bed_id=bed, diagnosis_name="历史住院",
                         status="discharged", admitted_at=datetime(2019, 5, 1), discharged_at=datetime(2019, 5, 10),
                         created_by=admin_id))
        db.commit()
    assert "MRQC12" not in _record_defects(client, admin, org, patient)   # 修前扣 MRQC12：按编号取到了补导的那条
