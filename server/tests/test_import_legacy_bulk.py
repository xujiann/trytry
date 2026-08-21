"""D1 存量迁移扩容：encounters/prescriptions/settlements/admissions 导入器。

覆盖：各导入器实导+幂等重跑（业务键查重）、金额 Money 口径校验、
错误行分流（errors.csv 落盘且不中断）、患者按身份证号/ehc_no 双通道解析。
"""
import csv
import sys
from datetime import datetime
from pathlib import Path

import pytest

from conftest import reset_database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_legacy import run_import  # noqa: E402

from app.database import SessionLocal
from app.models import (
    Admission,
    Bed,
    Encounter,
    Patient,
    Prescription,
    PrescriptionItem,
    Settlement,
    User,
    Ward,
)

SAMPLES = Path(__file__).resolve().parent.parent / "scripts" / "samples"


@pytest.fixture(scope="module", autouse=True)
def base_data():
    """机构+患者打底，并直建 admin 账号（created_by 经办解析用，不经 app 启动）。"""
    reset_database()
    db = SessionLocal()
    try:
        db.add(User(username="admin", password_hash="x", role="admin"))
        db.commit()
    finally:
        db.close()
    assert run_import("organizations", SAMPLES / "organizations.csv").errors == []
    assert run_import("patients", SAMPLES / "patients.csv").errors == []
    yield


def _count(model) -> int:
    db = SessionLocal()
    try:
        return db.query(model).count()
    finally:
        db.close()


def test_encounters_import_and_idempotent_rerun():
    report = run_import("encounters", SAMPLES / "encounters.csv")
    assert report.imported == 3 and report.errors == []
    db = SessionLocal()
    try:
        patient = db.query(Patient).filter(Patient.id_card == "320981196501012341").one()
        enc = db.query(Encounter).filter(Encounter.patient_id == patient.id).one()
        assert enc.diagnosis_code == "I10"
        assert enc.created_at == datetime(2026, 6, 1)  # created_at 取就诊日期
    finally:
        db.close()
    # 幂等：患者+机构+日期+诊断编码 查重，重跑全跳过
    rerun = run_import("encounters", SAMPLES / "encounters.csv")
    assert rerun.imported == 0 and rerun.skipped == 3
    assert _count(Encounter) == 3


def test_encounters_resolves_patient_by_ehc_no(tmp_path):
    db = SessionLocal()
    try:
        ehc = db.query(Patient).filter(Patient.id_card == "320981197203154322").one().ehc_no
    finally:
        db.close()
    by_ehc = tmp_path / "by_ehc.csv"
    by_ehc.write_text(
        "id_card,ehc_no,org_name,visit_date,diagnosis_code\n"
        f",{ehc},示例镇中心卫生院,2026-06-20,I10\n",
        encoding="utf-8",
    )
    assert run_import("encounters", by_ehc).imported == 1
    # 患者标识两列全空 → 错误行
    neither = tmp_path / "neither.csv"
    neither.write_text(
        "id_card,ehc_no,org_name,visit_date\n,,示例镇中心卫生院,2026-06-21\n", encoding="utf-8"
    )
    rep = run_import("encounters", neither)
    assert rep.imported == 0 and len(rep.errors) == 1 and "患者标识" in rep.errors[0][1]


def test_prescriptions_grouped_items_and_idempotent_rerun():
    report = run_import("prescriptions", SAMPLES / "prescriptions.csv")
    # 样例 3 行明细归 2 张处方（rx_no 相邻分组）
    assert report.imported == 2 and report.errors == []
    assert _count(Prescription) == 2 and _count(PrescriptionItem) == 3
    db = SessionLocal()
    try:
        rx = (
            db.query(Prescription)
            .filter(Prescription.diagnosis_name == "原发性高血压")
            .one()
        )
        assert {i.drug_code for i in rx.items} == {"C09AA04", "C03AA03"}
        assert rx.status == "approved"
    finally:
        db.close()
    # 幂等：患者+机构+日期+药品编码集合 查重（rx_no 不落库），重跑全跳过
    rerun = run_import("prescriptions", SAMPLES / "prescriptions.csv")
    assert rerun.imported == 0 and rerun.skipped == 2
    assert _count(Prescription) == 2 and _count(PrescriptionItem) == 3


def test_prescription_bad_detail_row_taints_whole_group(tmp_path):
    bad = tmp_path / "bad_rx.csv"
    bad.write_text(
        "rx_no,id_card,ehc_no,org_name,rx_date,drug_code,drug_name,daily_dose,days\n"
        "RXBAD1,320981196501012341,,示例镇中心卫生院,2026-07-01,X01,甲药,10,7\n"
        "RXBAD1,320981196501012341,,示例镇中心卫生院,2026-07-01,X02,乙药,-3,7\n",
        encoding="utf-8",
    )
    before = _count(Prescription)
    rep = run_import("prescriptions", bad)
    # 组内一行剂量非法 → 整组不导（半张处方比没有更糟）
    assert rep.imported == 0 and len(rep.errors) == 1
    assert _count(Prescription) == before


def test_settlements_money_validation_and_idempotency(tmp_path):
    report = run_import("settlements", SAMPLES / "settlements.csv")
    assert report.imported == 3 and report.errors == []
    db = SessionLocal()
    try:
        patient = db.query(Patient).filter(Patient.id_card == "320981197203154322").one()
        st = db.query(Settlement).filter(Settlement.patient_id == patient.id).one()
        # self_pay 留空 → 自动补 总额-医保
        assert st.total_amount == 132.00 and st.insurance_pay == 92.40
        assert st.self_pay == pytest.approx(39.60)
    finally:
        db.close()
    rerun = run_import("settlements", SAMPLES / "settlements.csv")
    assert rerun.imported == 0 and rerun.skipped == 3
    assert _count(Settlement) == 3

    # Money 口径：负数/三位小数/非数值/金额不平 全部拦下
    bad = tmp_path / "bad_money.csv"
    bad.write_text(
        "id_card,ehc_no,org_name,bill_type,settle_date,total_amount,insurance_pay,self_pay\n"
        "320981196501012341,,示例镇中心卫生院,outpatient,2026-07-01,-5,,\n"
        "320981196501012341,,示例镇中心卫生院,outpatient,2026-07-02,10.555,,\n"
        "320981196501012341,,示例镇中心卫生院,outpatient,2026-07-03,abc,,\n"
        "320981196501012341,,示例镇中心卫生院,outpatient,2026-07-04,100.00,30.00,30.00\n",
        encoding="utf-8",
    )
    rep = run_import("settlements", bad)
    assert rep.imported == 0 and len(rep.errors) == 4
    assert "不平" in rep.errors[3][1]


def test_admissions_autocreate_ward_bed_and_occupancy():
    report = run_import("admissions", SAMPLES / "admissions.csv")
    assert report.imported == 2 and report.errors == []
    db = SessionLocal()
    try:
        assert db.query(Ward).count() == 2 and db.query(Bed).count() == 2  # 幂等自动建
        occupied = db.query(Bed).filter(Bed.status == "occupied").all()
        assert len(occupied) == 1  # 在院行占床，历史出院行不占
        discharged = db.query(Admission).filter(Admission.status == "discharged").one()
        assert discharged.discharged_at is not None
    finally:
        db.close()
    rerun = run_import("admissions", SAMPLES / "admissions.csv")
    assert rerun.imported == 0 and rerun.skipped == 2
    assert _count(Admission) == 2


def test_error_rows_written_to_errors_csv_and_run_continues(tmp_path):
    bad = tmp_path / "bad_enc.csv"
    bad.write_text(
        "id_card,ehc_no,org_name,visit_date,diagnosis_code\n"
        "999999999999999999,,示例镇中心卫生院,2026-08-01,I10\n"   # 患者不存在
        "320981196501012341,,不存在的机构,2026-08-01,I10\n"        # 机构不存在
        "320981196501012341,,示例镇中心卫生院,2026-13-01,I10\n"    # 日期非法
        "320981196501012341,,示例镇中心卫生院,2026-08-02,Z99\n",   # 正常行
        encoding="utf-8",
    )
    errors_csv = tmp_path / "errors.csv"
    rep = run_import("encounters", bad, errors_csv=errors_csv)
    # 错误行分流继续跑：3 错 1 导，不中断
    assert rep.imported == 1 and len(rep.errors) == 3
    assert errors_csv.is_file()
    with errors_csv.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 3
    assert rows[0]["line_no"] == "2" and "患者不存在" in rows[0]["error"]
    assert rows[0]["id_card"] == "999999999999999999"  # 原始列保留，便于修正后重导
