"""块3：数据质控规则引擎——违规检出、合规不误报、规则停用后不检出、汇总与CRUD。"""
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def base(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "质控县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    client.post(
        "/api/users",
        json={"username": "qc_doc", "password": "pass123456", "role": "doctor", "org_id": org["id"]},
        headers=admin,
    )
    doctor = login(client, "qc_doc", "pass123456")
    # 合规患者：身份证校验位正确、姓名与出生日期完整
    good = client.post(
        "/api/patients",
        json={
            "name": "质控合规",
            "id_card": "330281199203046014",
            "gender": "男",
            "birth_date": "1992-03-04",
        },
        headers=admin,
    ).json()
    # 合规就诊：诊断编码取自 ICD-10 种子字典
    dict_entry = client.get("/api/dictionaries/diagnosis/entries?q=", headers=admin).json()[0]
    client.post(
        "/api/encounters",
        json={
            "patient_id": good["id"],
            "org_id": org["id"],
            "diagnosis_code": dict_entry["code"],
            "diagnosis_name": dict_entry["name"],
        },
        headers=doctor,
    )
    return {"org": org, "doctor": doctor, "good_patient": good, "dict_entry": dict_entry}


def _run(client, admin, params=""):
    resp = client.get(f"/api/dataquality/run{params}", headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _codes(data) -> set:
    return {v["rule_code"] for v in data["items"]}


def test_seed_rules_loaded(client, admin):
    rules = client.get("/api/dataquality/rules", headers=admin).json()
    assert len(rules) >= 15
    codes = {r["code"] for r in rules}
    assert {"QC001", "QC005", "QC008", "QC013", "QC015"} <= codes
    assert {r["rule_type"] for r in rules} <= {"required", "range", "enum", "cross_ref", "logic"}
    assert {r["severity"] for r in rules} <= {"error", "warn"}


def test_compliant_data_has_no_violation(client, admin, base):
    """合规数据不误报：仅有合规患者与合规就诊时，相关规则零违规。"""
    data = _run(client, admin)
    assert "QC001" not in _codes(data)  # 身份证校验位正确
    assert "QC002" not in _codes(data)
    assert "QC003" not in _codes(data)
    assert "QC004" not in _codes(data)
    assert "QC005" not in _codes(data)  # 诊断编码在字典内


def test_required_and_logic_rules_detect_violations(client, admin, base):
    """构造违规数据：身份证校验位错误、出生日期缺失、诊断编码缺失/不在字典。"""
    from app.database import SessionLocal
    from app.models import Encounter, Patient

    db = SessionLocal()
    try:
        bad = Patient(
            ehc_no="QC-BAD-001",
            name="质控违规",
            id_card="330281199203046010",  # 校验位错误
            gender="男",
            birth_date="",  # 出生日期缺失
        )
        db.add(bad)
        db.commit()
        bad_id = bad.id
        missing_code = Encounter(
            patient_id=base["good_patient"]["id"],
            org_id=base["org"]["id"],
            diagnosis_code="",
            diagnosis_name="未编码诊断",
        )
        unknown_code = Encounter(
            patient_id=base["good_patient"]["id"],
            org_id=base["org"]["id"],
            diagnosis_code="ZZ99.999",  # 字典内不存在
            diagnosis_name="字典外诊断",
        )
        db.add_all([missing_code, unknown_code])
        db.commit()
        missing_id, unknown_id = missing_code.id, unknown_code.id
    finally:
        db.close()
    data = _run(client, admin)
    by_code = {}
    for v in data["items"]:
        by_code.setdefault(v["rule_code"], []).append(v)
    assert {v["record_id"] for v in by_code["QC001"]} == {bad_id}
    assert "校验位" in by_code["QC001"][0]["message"]
    assert by_code["QC001"][0]["severity"] == "error"
    assert {v["record_id"] for v in by_code["QC003"]} == {bad_id}
    assert {v["record_id"] for v in by_code["QC004"]} == {missing_id}
    assert {v["record_id"] for v in by_code["QC005"]} == {unknown_id}  # 空编码由 QC004 负责，不重复报


def test_range_rule_detects_dose_and_days(client, admin, base):
    from app.database import SessionLocal
    from app.models import Prescription, PrescriptionItem

    db = SessionLocal()
    try:
        rx = Prescription(
            patient_id=base["good_patient"]["id"],
            org_id=base["org"]["id"],
            diagnosis_name="",  # QC010 警告
            created_by=1,
        )
        db.add(rx)
        db.commit()
        bad_dose = PrescriptionItem(
            prescription_id=rx.id, drug_code="QC-D1", drug_name="零剂量药", daily_dose=0, days=7
        )
        bad_days = PrescriptionItem(
            prescription_id=rx.id, drug_code="QC-D2", drug_name="超天数药", daily_dose=5, days=365
        )
        ok_item = PrescriptionItem(
            prescription_id=rx.id, drug_code="QC-D3", drug_name="合规药", daily_dose=5, days=7
        )
        db.add_all([bad_dose, bad_days, ok_item])
        db.commit()
        ids = (rx.id, bad_dose.id, bad_days.id, ok_item.id)
    finally:
        db.close()
    rx_id, dose_id, days_id, ok_id = ids
    dose = _run(client, admin, "?rule_code=QC008")
    assert {v["record_id"] for v in dose["items"]} == {dose_id}  # 合规行不误报
    days = _run(client, admin, "?rule_code=QC009")
    assert {v["record_id"] for v in days["items"]} == {days_id}
    assert ok_id not in {v["record_id"] for v in days["items"]}
    rx_diag = _run(client, admin, "?rule_code=QC010")
    assert {v["record_id"] for v in rx_diag["items"]} == {rx_id}
    assert rx_diag["warn_total"] == rx_diag["total"]  # QC010 为警告级


def test_cross_ref_and_followup_logic_rules(client, admin, base):
    from app.database import SessionLocal
    from app.models import ChronicPatient, FollowUp

    db = SessionLocal()
    try:
        good_chronic = ChronicPatient(
            patient_id=base["good_patient"]["id"],
            disease="hypertension",
            level=1,
            managed_by_org_id=base["org"]["id"],
            next_due=(date.today() + timedelta(days=30)).isoformat(),
        )
        bad_chronic = ChronicPatient(
            patient_id=base["good_patient"]["id"],
            disease="not_in_catalog",  # 不在病种目录
            level=1,
            managed_by_org_id=base["org"]["id"],
            next_due="",  # QC011 警告
        )
        db.add_all([good_chronic, bad_chronic])
        db.commit()
        ok_fu = FollowUp(chronic_id=good_chronic.id, sbp=135, dbp=85)
        bad_fu = FollowUp(chronic_id=good_chronic.id, sbp=None, dbp=None)  # 高血压缺指标
        db.add_all([ok_fu, bad_fu])
        db.commit()
        ids = (good_chronic.id, bad_chronic.id, ok_fu.id, bad_fu.id)
    finally:
        db.close()
    good_id, bad_id, ok_fu_id, bad_fu_id = ids
    cross = _run(client, admin, "?rule_code=QC012")
    assert {v["record_id"] for v in cross["items"]} == {bad_id}
    assert good_id not in {v["record_id"] for v in cross["items"]}
    due = _run(client, admin, "?rule_code=QC011")
    assert {v["record_id"] for v in due["items"]} == {bad_id}
    fu = _run(client, admin, "?rule_code=QC013")
    assert {v["record_id"] for v in fu["items"]} == {bad_fu_id}
    assert ok_fu_id not in {v["record_id"] for v in fu["items"]}


def test_date_logic_rules(client, admin, base):
    from app.database import SessionLocal
    from app.models import Admission, Bed, InfectiousCase, Ward

    db = SessionLocal()
    try:
        ward = Ward(org_id=base["org"]["id"], name="质控病区")
        db.add(ward)
        db.commit()
        bed = Bed(ward_id=ward.id, bed_no="QC-01")
        db.add(bed)
        db.commit()
        bad_adm = Admission(
            patient_id=base["good_patient"]["id"],
            org_id=base["org"]["id"],
            ward_id=ward.id,
            bed_id=bed.id,
            status="discharged",
            admitted_at=datetime(2026, 5, 10, 9, 0),
            discharged_at=datetime(2026, 5, 8, 9, 0),  # 出院早于入院
            created_by=1,
        )
        ok_adm = Admission(
            patient_id=base["good_patient"]["id"],
            org_id=base["org"]["id"],
            ward_id=ward.id,
            bed_id=bed.id,
            status="discharged",
            admitted_at=datetime(2026, 5, 1, 9, 0),
            discharged_at=datetime(2026, 5, 6, 9, 0),
            created_by=1,
        )
        future_case = InfectiousCase(
            org_id=base["org"]["id"],
            disease_code="A01",
            disease_name="伤寒",
            category="B",
            onset_date=(date.today() + timedelta(days=3)).isoformat(),
        )
        past_case = InfectiousCase(
            org_id=base["org"]["id"],
            disease_code="A01",
            disease_name="伤寒",
            category="B",
            onset_date=(date.today() - timedelta(days=3)).isoformat(),
        )
        db.add_all([bad_adm, ok_adm, future_case, past_case])
        db.commit()
        ids = (bad_adm.id, ok_adm.id, future_case.id, past_case.id)
    finally:
        db.close()
    bad_adm_id, ok_adm_id, future_id, past_id = ids
    adm = _run(client, admin, "?rule_code=QC014")
    assert {v["record_id"] for v in adm["items"]} == {bad_adm_id}
    assert ok_adm_id not in {v["record_id"] for v in adm["items"]}
    inf = _run(client, admin, "?rule_code=QC015")
    assert {v["record_id"] for v in inf["items"]} == {future_id}
    assert past_id not in {v["record_id"] for v in inf["items"]}


def test_summary_matches_run(client, admin, base):
    summary = client.get("/api/dataquality/summary", headers=admin).json()
    run = _run(client, admin, "?limit=1000")
    assert summary["total"] == run["total"]
    assert summary["by_severity"]["error"] == run["error_total"]
    assert summary["by_severity"]["warn"] == run["warn_total"]
    assert summary["rules_checked"] >= 15
    per_rule = {r["rule_code"]: r["violations"] for r in summary["by_rule"]}
    counted = {}
    for v in run["items"]:
        counted[v["rule_code"]] = counted.get(v["rule_code"], 0) + 1
    for code, n in counted.items():
        assert per_rule[code] == n, code


def test_deactivated_rule_is_not_scanned(client, admin, base):
    rules = {r["code"]: r for r in client.get("/api/dataquality/rules", headers=admin).json()}
    qc001 = rules["QC001"]
    before = _run(client, admin, "?rule_code=QC001")
    assert before["total"] > 0
    resp = client.patch(
        f"/api/dataquality/rules/{qc001['id']}", json={"active": False}, headers=admin
    )
    assert resp.status_code == 200, resp.text
    after = _run(client, admin, "?rule_code=QC001")
    assert after["total"] == 0  # 停用后不再检出
    assert "QC001" not in {r["rule_code"] for r in client.get("/api/dataquality/summary", headers=admin).json()["by_rule"]}
    client.patch(f"/api/dataquality/rules/{qc001['id']}", json={"active": True}, headers=admin)
    assert _run(client, admin, "?rule_code=QC001")["total"] == before["total"]


def test_rule_crud_admin_only(client, admin, base):
    resp = client.post(
        "/api/dataquality/rules",
        json={
            "code": "QC900",
            "name": "证明类型须在枚举内",
            "target_table": "medical_certs",
            "rule_type": "enum",
            "config": {"field": "cert_type", "values": ["birth", "death", "defect"]},
            "severity": "error",
        },
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    rule_id = resp.json()["id"]
    assert client.post(
        "/api/dataquality/rules",
        json={
            "code": "QC900",
            "name": "重复编码",
            "target_table": "medical_certs",
            "rule_type": "enum",
            "config": {"field": "cert_type", "values": []},
        },
        headers=admin,
    ).status_code == 409
    assert client.post(
        "/api/dataquality/rules",
        json={
            "code": "QC901",
            "name": "未登记表",
            "target_table": "not_a_table",
            "rule_type": "required",
            "config": {"field": "x"},
        },
        headers=admin,
    ).status_code == 422
    assert client.patch(
        f"/api/dataquality/rules/{rule_id}", json={"severity": "warn"}, headers=admin
    ).json()["severity"] == "warn"
    assert client.patch(
        "/api/dataquality/rules/99999", json={"active": False}, headers=admin
    ).status_code == 404
    assert client.delete(f"/api/dataquality/rules/{rule_id}", headers=admin).status_code == 200
    doctor = base["doctor"]
    assert client.post(
        "/api/dataquality/rules",
        json={
            "code": "QC902",
            "name": "越权新增",
            "target_table": "patients",
            "rule_type": "required",
            "config": {"field": "name"},
        },
        headers=doctor,
    ).status_code == 403
    assert client.get("/api/dataquality/rules", headers=doctor).status_code == 200  # 只读放行


def test_run_filters_and_pagination(client, admin, base):
    errors = _run(client, admin, "?severity=error&limit=1000")
    assert errors["total"] == errors["error_total"] and errors["warn_total"] == 0
    by_table = _run(client, admin, "?target_table=patients&limit=1000")
    assert {v["table"] for v in by_table["items"]} == {"patients"}
    page = _run(client, admin, "?offset=0&limit=1")
    assert len(page["items"]) == 1 and page["total"] > 1


def test_requires_login(client):
    assert client.get("/api/dataquality/run").status_code == 401
    assert client.get("/api/dataquality/summary").status_code == 401
