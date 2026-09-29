"""存量患者导入的出生日期不得晚于今天，与界面建档同一句（P2-739，第十九批「导入 / 入站对接 vs 界面录入」扫描 K3-14 的
存量导入部分；入站 HL7 / FHIR 一侧随 P1-61 剩余部分待裁定）。

P2-713 只拦了界面建档与更正；导入脚本只查格式，2070-01-01 照导——年龄算成负数，慢专病「未满 18 岁不纳入」把成年人排除、
知情同意代录要求监护人（算年龄处已按 P2-713 当作「不知道」，后果同 P1-61）。
"""
import sys
from datetime import date
from pathlib import Path

import pytest

from conftest import freeze_business_date, reset_database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_legacy import run_import  # noqa: E402

from app.database import SessionLocal
from app.models import Patient

HEADER = "name,id_card,gender,birth_date\n"


@pytest.fixture(scope="module", autouse=True)
def world():
    reset_database()


def test_将来的出生日期记错误行_今天出生的照导(tmp_path):
    path = tmp_path / "patients.csv"
    path.write_text(HEADER + "P2739 甲,330102207001012739,男,2070-01-01\n"
                             "P2739 乙,330102202609292738,女,2026-09-29\n", encoding="utf-8")
    with freeze_business_date(date(2026, 9, 29)):
        rep = run_import("patients", path)
    assert rep.imported == 1, rep.errors
    assert [(line, "不得晚于今天" in msg) for line, msg in rep.errors] == [(2, True)]   # 修前两行都导入
    with SessionLocal() as db:
        assert [p.name for p in db.query(Patient).filter(Patient.name.like("P2739%")).all()] == ["P2739 乙"]
