"""医生 360 的就诊、处方、报告各段带业务时间，报告带检查项目与危急值处置状态（P2-1196，第三十四批「患者全景与时间轴」扫描 L4-3）。

修前 `GET /api/archive/{ehc_no}` 的就诊行只有 id / org_id / encounter_type / diagnosis_name / summary，处方行只有
id / diagnosis_name / status，报告行只有 id / request_id / conclusion / critical：同一响应里结算、体检两段带日期，这三段
一个日期都没有——`_section` 按业务时刻截取（P2-846），看的人却看不到排序依据。医生移动端速查（`m/doctor.js`）只印
「肌钙蛋白I 2.3ng/ml 升高 / 危急值：是」，分不清是哪天、哪项检查、危急值处置了没有。

修后只加键、既有键一个不动（向后兼容）：就诊与处方加 `created_at`，报告加 `item_name` / `reported_at` / `critical_status`，
时间与同一响应里结算段的 `created_at` 同一写法（`isoformat()`）；速查卡片印出日期、检查项目与危急值处置状态。
键集合的特征化网 `test_archive_360_contract.py` 按增量字段同步。
"""
from datetime import datetime
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import Encounter, ExamReport, Prescription, User
from jssrc import strip_comments

DOCTOR_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "m" / "doctor.js"
#: 上线后补导的历史就诊 / 处方：created_at 照写业务时刻（`scripts/import_legacy.py`），编号更大
IMPORTED_AT = datetime(2019, 6, 15, 9, 30)


def _ok(resp):
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


@pytest.fixture(scope="module")
def world(client, admin):
    org = _ok(client.post("/api/organizations", headers=admin, json={
        "name": "P21196 县医院", "org_type": "lead_hospital", "level": "county"}))["id"]
    patient = _ok(client.post("/api/patients", headers=admin, json={
        "name": "P21196 老李", "id_card": "330102196001011196"}))
    encounter = _ok(client.post("/api/encounters", headers=admin, json={
        "patient_id": patient["id"], "org_id": org, "diagnosis_name": "胸痛待查", "summary": "门诊首诊"}))
    prescription = _ok(client.post("/api/prescriptions", headers=admin, json={
        "patient_id": patient["id"], "org_id": org, "diagnosis_name": "高血压",
        "items": [{"drug_code": "P21196-D1", "drug_name": "氨氯地平片", "daily_dose": 5, "days": 30}]}))
    reports = {}
    for key, item_code, item_name, conclusion, critical in (
        ("critical", "CTNI", "肌钙蛋白I", "肌钙蛋白I 2.3ng/ml 升高", True),
        ("normal", "CT", "胸部CT", "未见异常", False),
    ):
        request = _ok(client.post("/api/exams", headers=admin, json={
            "patient_id": patient["id"], "from_org_id": org, "center_type": "lab",
            "item_code": item_code, "item_name": item_name}))
        report = _ok(client.post(f"/api/exams/{request['id']}/report", headers=admin, json={
            "conclusion": conclusion, "critical": critical}))
        reports[key] = {"request": request["id"], "report": report["id"]}
    # 危急值走到「已接收、待处置」：360 要说得出处置到了哪一步
    _ok(client.post(f"/api/exams/reports/{reports['critical']['report']}/acknowledge", headers=admin))
    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        imported_encounter = Encounter(patient_id=patient["id"], org_id=org, diagnosis_name="高血压复诊",
                                       created_at=IMPORTED_AT)
        imported_prescription = Prescription(patient_id=patient["id"], org_id=org, diagnosis_name="历史处方",
                                             created_by=admin_id, created_at=IMPORTED_AT)
        db.add_all([imported_encounter, imported_prescription])
        db.commit()
        imported = {"imported_encounter": imported_encounter.id, "imported_prescription": imported_prescription.id}
    view = client.get(f"/api/archive/{patient['ehc_no']}", headers=admin)
    assert view.status_code == 200, view.text
    return {"encounter": encounter["id"], "prescription": prescription["id"], "reports": reports, **imported,
            "view": view.json()}


def test_就诊行带就诊时刻_补导的历史就诊印的是就诊日(world):
    rows = {row["id"]: row for row in world["view"]["encounters"]}
    with SessionLocal() as db:
        today = db.get(Encounter, world["encounter"])
        assert rows[today.id]["created_at"] == today.created_at.isoformat()   # 修前没有这个键
    assert rows[world["imported_encounter"]]["created_at"] == IMPORTED_AT.isoformat()
    # 既有键与取值不变
    assert {k: rows[world["encounter"]][k] for k in ("encounter_type", "diagnosis_name", "summary")} == {
        "encounter_type": "outpatient", "diagnosis_name": "胸痛待查", "summary": "门诊首诊"}


def test_处方行带开方时刻(world):
    rows = {row["id"]: row for row in world["view"]["prescriptions"]}
    with SessionLocal() as db:
        rx = db.get(Prescription, world["prescription"])
        assert rows[rx.id]["created_at"] == rx.created_at.isoformat()   # 修前没有这个键
    assert rows[world["imported_prescription"]]["created_at"] == IMPORTED_AT.isoformat()
    assert rows[world["prescription"]]["diagnosis_name"] == "高血压"
    assert rows[world["prescription"]]["status"] == "auto_passed"


def test_报告行带检查项目_报告时刻与危急值处置状态(world):
    rows = {row["id"]: row for row in world["view"]["exam_reports"]}
    critical = rows[world["reports"]["critical"]["report"]]
    normal = rows[world["reports"]["normal"]["report"]]
    # 修前这三个键都没有：看不出是哪项检查、哪天出的、危急值处置了没有
    assert critical["item_name"] == "肌钙蛋白I" and normal["item_name"] == "胸部CT"
    assert critical["critical_status"] == "acknowledged" and normal["critical_status"] == ""
    with SessionLocal() as db:
        for row in (critical, normal):
            assert row["reported_at"] == db.get(ExamReport, row["id"]).reported_at.isoformat()
    # 既有键与取值不变
    assert {k: critical[k] for k in ("request_id", "conclusion", "critical")} == {
        "request_id": world["reports"]["critical"]["request"], "conclusion": "肌钙蛋白I 2.3ng/ml 升高", "critical": True}
    assert normal["critical"] is False


def test_医生移动端速查_就诊与报告卡片印出日期和检查项目():
    src = strip_comments(DOCTOR_JS.read_text(encoding="utf-8"))
    start = src.index('$("#pt-form").addEventListener("submit"')
    body = src[start:src.index("\n});\n", start)]
    encounters = body[body.index("const encounters"):body.index("const reports")]
    reports = body[body.index("const reports"):body.index('$("#pt-result").innerHTML')]
    assert "en.created_at" in encounters, encounters   # 修前只印诊断 / 类型 / 摘要
    for needle in ("r.item_name", "r.reported_at", "statusTag(CRITICAL_TAGS, r.critical_status)"):
        assert needle in reports, reports   # 修前只印结论与「危急值：是」
