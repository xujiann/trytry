"""存量导入认证件号末位校验码 X 的两种写法（P1-197，第十二批「批量 vs 单条」扫描 Z4-4）。

平台建档、HL7 / FHIR 入站早就把 `…X` 与 `…x` 认作同一个人（P1-114，`patients.id_card_variants`）；存量导入原先按原样
比对：表单上建过 `…X` 的人，旧系统导出里写成 `…x` 就再建一本档案，之后按 `…x` 导的就诊、慢病档案全挂到那本新档上；
按 `…X` 导入过的库里，写成 `…x` 的就诊行则报「患者不存在」。
"""
import sys
from pathlib import Path

import pytest

from conftest import reset_database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_legacy import run_import  # noqa: E402

from app.chronic_seed import SEED_CHRONIC_DISEASE_TYPES
from app.database import SessionLocal
from app.models import ChronicDiseaseType, ChronicPatient, Encounter, Organization, Patient

UPPER = "11010519491231197X"
LOWER = UPPER[:-1] + "x"


@pytest.fixture(scope="module", autouse=True)
def world():
    reset_database()
    with SessionLocal() as db:
        db.add(Organization(name="P1197 卫生院", org_type="township", level="township"))
        db.add_all(ChronicDiseaseType(**seed) for seed in SEED_CHRONIC_DISEASE_TYPES)   # 慢病档案导入按目录校验病种
        db.commit()


def _csv(tmp_path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _patients() -> list[int]:
    with SessionLocal() as db:
        return [pid for (pid,) in db.query(Patient.id).filter(Patient.id_card.in_([UPPER, LOWER])).order_by(Patient.id)]


def test_写成小写x的同一个人_导入时幂等跳过而不是再建一本(tmp_path):
    first = run_import("patients", _csv(tmp_path, "a.csv", f"name,id_card,gender,birth_date,phone\n张三,{UPPER},男,,\n"))
    assert first.imported == 1 and first.errors == []
    again = run_import("patients", _csv(tmp_path, "b.csv", f"name,id_card,gender,birth_date,phone\n张三,{LOWER},男,,\n"))
    assert (again.imported, again.skipped, again.errors) == (0, 1, [])   # 修前 imported == 1
    assert len(_patients()) == 1


def test_同批里两种写法算同批重复(tmp_path):
    other = "11010519491231198" + "X"
    rep = run_import("patients", _csv(tmp_path, "c.csv", (
        "name,id_card,gender,birth_date,phone\n"
        f"李四,{other},男,,\n"
        f"李四,{other[:-1]}x,男,,\n")))
    assert rep.imported == 1 and len(rep.errors) == 1 and "同批内身份证号重复" in rep.errors[0][1], rep.errors


def test_按小写x导就诊与慢病档案_挂到同一本档案上(tmp_path):
    (pid,) = _patients()
    enc = run_import("encounters", _csv(tmp_path, "e.csv", (
        "id_card,org_name,visit_date,diagnosis_name\n"
        f"{LOWER},P1197 卫生院,2026-01-05,高血压\n")))
    assert enc.imported == 1 and enc.errors == [], enc.errors
    chronic = run_import("chronic", _csv(tmp_path, "f.csv", (
        "id_card,disease,level,managed_by_org,next_due\n"
        f"{LOWER},hypertension,1,P1197 卫生院,\n")))
    assert chronic.imported == 1 and chronic.errors == [], chronic.errors   # 修前「患者不存在」
    with SessionLocal() as db:
        assert [e.patient_id for e in db.query(Encounter).all()] == [pid]
        assert [c.patient_id for c in db.query(ChronicPatient).all()] == [pid]
