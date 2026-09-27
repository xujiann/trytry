"""驾驶舱下钻明细印后端给的中文文案，不印编码（P2-646，第十四批「导出 / 打印 vs 页面」扫描 R1-5）。

下钻明细表按 `fields` 逐列取值渲染，原先编码类字段原样上表：退回处方的状态是 rejected、上转单是 up / accepted、
远程诊断是 imaging / reported、医废是 sharp / stored、慢病病种是 hypertension、传染病分类是 A、就诊类型是 outpatient；
危急值存量空串还被编成 pending（危急值页叫它「待回填」）。同样的数据在各业务页上都是中文（§13「状态文案取自后端」）。

修法：行里另给 `*_name`（文案表用业务端那几张，与打印件同一份；病种名取慢病病种目录），`fields` 指向它；编码本身
照旧留在行里、不改值。
"""
from datetime import date, datetime, timedelta

import pytest

from app.database import SessionLocal
from app.models import (
    ChronicPatient,
    Encounter,
    ExamReport,
    ExamRequest,
    InfectiousCase,
    MedicalWaste,
    Prescription,
    Referral,
    User,
)

#: 这些编码列不再直接上表（上表的是它们的 `*_name`）
CODE_FIELDS = {"status", "direction", "center_type", "waste_type", "category", "encounter_type", "critical_status",
               "disease"}


@pytest.fixture(scope="module")
def ids(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2646 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2646 患者", "id_card": "330281198806062646"}).json()["id"]
    old = str(date.today() - timedelta(days=9))
    with SessionLocal() as db:
        doctor = db.query(User).filter(User.username == "admin").one()
        request, other = (ExamRequest(patient_id=patient, from_org_id=org, center_type="ecg", item_code="ECG",
                                      item_name="P2646 心电", status="reported", created_by=doctor.id)
                          for _ in range(2))
        db.add_all([request, other])
        db.flush()
        legacy = ExamReport(request_id=request.id, conclusion="P2646 存量", critical=True, critical_status="",
                            reported_by="李医生", reported_at=datetime.utcnow())
        acked = ExamReport(request_id=other.id, conclusion="P2646 已确认", critical=True,
                           critical_status="acknowledged", reported_by="李医生", reported_at=datetime.utcnow())
        known = ChronicPatient(patient_id=patient, disease="hypertension", level=2, managed_by_org_id=org, next_due=old)
        custom = ChronicPatient(patient_id=patient, disease="p2646_x", level=1, managed_by_org_id=org, next_due=old)
        waste = MedicalWaste(org_id=org, waste_type="sharp", weight_kg=1.5, status="stored", trace_code="P2646-W",
                             collected_date=old)
        case = InfectiousCase(org_id=org, disease_code="A00", disease_name="霍乱", category="A",
                              onset_date=str(date.today()))
        referral = Referral(patient_id=patient, from_org_id=org, to_org_id=org, direction="up", reason="P2646",
                            status="accepted", created_by=doctor.id)
        encounter = Encounter(patient_id=patient, org_id=org, encounter_type="outpatient", diagnosis_name="P2646 诊断")
        rx = Prescription(patient_id=patient, org_id=org, diagnosis_name="P2646 处方", status="rejected",
                          review_comment="剂量超限", created_by=doctor.id)
        rows = [legacy, acked, known, custom, waste, case, referral, encounter, rx]
        db.add_all(rows)
        db.commit()
        return {"request": request.id, "legacy": legacy.id, "acked": acked.id, "known": known.id,
                "custom": custom.id, "waste": waste.id, "case": case.id, "referral": referral.id,
                "encounter": encounter.id, "rx": rx.id}


def _drill(client, admin, metric):
    body = client.get(f"/api/metrics/drilldown?metric={metric}&limit=500", headers=admin).json()
    return body, {row["id"]: row for row in body["items"]}


def _shown(body, row):
    return dict(zip(body["columns"], (row[f] for f in body["fields"])))


def test_上表的字段里没有编码列(client, admin, ids):
    for metric in [m["metric"] for m in client.get("/api/metrics/drilldown-metrics", headers=admin).json()]:
        body, _ = _drill(client, admin, metric)
        assert not CODE_FIELDS & set(body["fields"]), (metric, body["fields"])   # 修前 11 个指标都有


def test_各指标的编码列印中文(client, admin, ids):
    body, rows = _drill(client, admin, "critical_values")
    assert _shown(body, rows[ids["legacy"]])["闭环状态"] == "待回填"   # 修前 pending
    assert _shown(body, rows[ids["acked"]])["闭环状态"] == "已确认"
    body, rows = _drill(client, admin, "chronic_overdue")
    assert _shown(body, rows[ids["known"]])["病种"] == "高血压"
    assert _shown(body, rows[ids["custom"]])["病种"] == "p2646_x"   # 目录里没有的原样回显
    body, rows = _drill(client, admin, "medwaste_overdue")
    assert (_shown(body, rows[ids["waste"]])["类别"], _shown(body, rows[ids["waste"]])["状态"]) == ("损伤性", "已暂存")
    body, rows = _drill(client, admin, "infectious_recent")
    assert _shown(body, rows[ids["case"]])["分类"] == "甲类"
    body, rows = _drill(client, admin, "referrals_up")
    assert (_shown(body, rows[ids["referral"]])["方向"], _shown(body, rows[ids["referral"]])["状态"]) == ("上转", "已接诊")
    body, rows = _drill(client, admin, "grassroots_encounters")
    assert _shown(body, rows[ids["encounter"]])["就诊类型"] == "门诊"
    body, rows = _drill(client, admin, "reported_exams")
    assert (_shown(body, rows[ids["request"]])["中心"], _shown(body, rows[ids["request"]])["状态"]) == ("心电", "已报告")
    body, rows = _drill(client, admin, "rejected_prescriptions")
    assert _shown(body, rows[ids["rx"]])["状态"] == "已退回"


def test_编码照旧留在行里_不改值(client, admin, ids):
    _, rows = _drill(client, admin, "critical_values")
    assert rows[ids["legacy"]]["critical_status"] == "pending"   # 既有键的值不动（向后兼容）
    _, rows = _drill(client, admin, "referrals_up")
    assert (rows[ids["referral"]]["direction"], rows[ids["referral"]]["status"]) == ("up", "accepted")
