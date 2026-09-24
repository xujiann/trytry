"""块2：存量数据迁移工具（scripts/import_legacy.py）测试。

覆盖：dry-run 校验不落库、实导幂等、错误行明细、外键（机构/患者）解析。
"""
import sys
from pathlib import Path

import pytest

from conftest import reset_database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_legacy import run_import  # noqa: E402

from app.database import SessionLocal
from app.models import ChronicPatient, Employee, Encounter, Organization, Patient

SAMPLES = Path(__file__).resolve().parent.parent / "scripts" / "samples"


@pytest.fixture(scope="module", autouse=True)
def fresh_db():
    reset_database()
    yield


def _count(model) -> int:
    db = SessionLocal()
    try:
        return db.query(model).count()
    finally:
        db.close()


def test_dry_run_reports_without_writing():
    report = run_import("organizations", SAMPLES / "organizations.csv", dry_run=True)
    assert report.dry_run is True
    assert report.imported == 3 and report.skipped == 0 and report.errors == []
    # dry-run 不落库
    assert _count(Organization) == 0


def test_real_import_then_idempotent_rerun():
    report = run_import("organizations", SAMPLES / "organizations.csv")
    assert report.imported == 3 and report.errors == []
    assert _count(Organization) == 3
    # 上下级关系解析（同批内先导入上级）
    db = SessionLocal()
    try:
        township = db.query(Organization).filter(Organization.name == "示例镇中心卫生院").one()
        county = db.query(Organization).filter(Organization.name == "示例县人民医院").one()
        assert township.parent_id == county.id
    finally:
        db.close()

    # 重跑：全部幂等跳过
    rerun = run_import("organizations", SAMPLES / "organizations.csv")
    assert rerun.imported == 0 and rerun.skipped == 3 and rerun.errors == []
    assert _count(Organization) == 3


def test_patients_empi_idempotent(tmp_path):
    report = run_import("patients", SAMPLES / "patients.csv")
    assert report.imported == 3 and report.errors == []
    db = SessionLocal()
    try:
        p = db.query(Patient).filter(Patient.id_card == "320981196501012341").one()
        assert p.ehc_no.startswith("EHC") and p.gender == "男"
    finally:
        db.close()

    # 同身份证号重导：EMPI 幂等跳过，不重复建档
    rerun = run_import("patients", SAMPLES / "patients.csv")
    assert rerun.imported == 0 and rerun.skipped == 3
    assert _count(Patient) == 3

    # 同批内重复身份证号 → 错误行
    dup = tmp_path / "dup.csv"
    dup.write_text(
        "name,id_card,gender,birth_date,phone\n"
        "甲,320981199901011111,男,,\n"
        "乙,320981199901011111,女,,\n",
        encoding="utf-8",
    )
    rep = run_import("patients", dup)
    assert rep.imported == 1 and len(rep.errors) == 1
    assert rep.errors[0][0] == 3 and "重复" in rep.errors[0][1]


def test_chronic_and_employees_with_fk_resolution():
    chronic = run_import("chronic", SAMPLES / "chronic.csv")
    assert chronic.imported == 2 and chronic.errors == []
    assert run_import("chronic", SAMPLES / "chronic.csv").skipped == 2
    assert _count(ChronicPatient) == 2

    emp = run_import("employees", SAMPLES / "employees.csv")
    assert emp.imported == 3 and emp.errors == []
    assert run_import("employees", SAMPLES / "employees.csv").skipped == 3
    assert _count(Employee) == 3


def test_日期列与平台同一个真源_ISO变体既不原样落库也不再重导翻倍(tmp_path):
    """P1-61：`_valid_date` 原先用 `date.fromisoformat`——Python 3.11 起它还接受 `19870101`、
    `1987-W01-1`（ISO 周日期），脚本头注与报错文案却写着 YYYY-MM-DD。后果两种：字符串列
    （出生日期）原样落库；时间戳列（就诊等）虽被解析，幂等键却拿原串去比库里 `YYYY-MM-DD`
    形状的键——对不上，同一份文件每重导一次就多一份（实测 2 → 3 → 4）。"""
    run_import("organizations", SAMPLES / "organizations.csv")  # 幂等：前面导过就全部跳过
    run_import("patients", SAMPLES / "patients.csv")

    people = tmp_path / "iso_variants.csv"
    people.write_text(
        "name,id_card,gender,birth_date,phone\n"
        "己,320981198701014444,男,19870101,\n"
        "庚,320981198701025555,男,1987-W01-1,\n"
        "辛,320981198702286666,女,1987-02-28,\n",
        encoding="utf-8",
    )
    rep = run_import("patients", people, dry_run=True)
    assert rep.imported == 1 and [line for line, _ in rep.errors] == [2, 3]
    assert all("birth_date 格式非法" in msg for _, msg in rep.errors), rep.errors

    visits = tmp_path / "compact_visit.csv"
    visits.write_text(
        "id_card,ehc_no,org_name,visit_date,encounter_type,doctor_name,diagnosis_code,"
        "diagnosis_name,summary\n"
        "320981197203154322,,示例镇中心卫生院,20260603,outpatient,钱医生,E11,2型糖尿病,紧凑写法\n",
        encoding="utf-8",
    )
    before = _count(Encounter)
    for _ in range(2):
        rep = run_import("encounters", visits)
        assert rep.imported == 0 and len(rep.errors) == 1, rep.errors
    assert _count(Encounter) == before, "紧凑写法的就诊不该落库，更不该每重导一次多一份"


def test_error_rows_detail(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "name,id_card,gender,birth_date,phone\n"
        ",320981200001011111,男,,\n"          # 缺姓名
        "丙,123,男,,\n"                        # 身份证长度非法
        "丁,320981200001012222,男,2000-13-99,\n"  # 生日格式非法
        "戊,320981200001013333,女,2000-01-01,13900000000\n",  # 正常行
        encoding="utf-8",
    )
    # dry-run 报告错误明细且不落库
    before = _count(Patient)
    rep = run_import("patients", bad, dry_run=True)
    assert rep.imported == 1 and len(rep.errors) == 3
    assert [line for line, _ in rep.errors] == [2, 3, 4]
    assert _count(Patient) == before

    summary = rep.summary()
    assert "dry-run" in summary and "错误: 3 行" in summary

    # 慢病：患者/机构不存在 → 错误行
    orphan = tmp_path / "orphan.csv"
    orphan.write_text(
        "id_card,disease,level,managed_by_org,next_due\n"
        "999999999999999999,hypertension,1,不存在的机构,\n",
        encoding="utf-8",
    )
    rep2 = run_import("chronic", orphan)
    assert rep2.imported == 0 and len(rep2.errors) == 1


def test_机构名与建档同口径_单字与超长都进错误行(tmp_path):
    """P2-40：导入原先只查机构名非空，建档（`OrganizationCreate`）要 2～128 字。导进一个单字机构名，
    机构清单的出参按建档约束校验，`GET /api/organizations` 对所有人 500（修前实测）；超长的在 PG 上
    写库即失败，整批导入中断。两种都该进错误行，别的行照导。"""
    names = tmp_path / "org_names.csv"
    names.write_text(
        "name,org_type,level,parent_name,address\n"
        "院,village,village,,\n"
        f"{'长' * 129},village,village,,\n"
        "示例村卫生室二,village,village,,\n"
        f"{'满' * 128},village,village,,\n",
        encoding="utf-8",
    )
    rep = run_import("organizations", names, dry_run=True)
    assert rep.imported == 2 and [line for line, _ in rep.errors] == [2, 3], rep.errors
    assert all("name 长度非法" in msg for _, msg in rep.errors), rep.errors
