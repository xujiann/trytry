"""块3：标准字典导入工具（scripts/import_dictionary.py）与启动种子扩充测试。

覆盖：启动种子（ICD-10 100条/药品50条，幂等）、CSV dry-run 不落库、
实导与幂等重跑、错误行明细、进度输出、API 检索联动。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_dictionary import run_import  # noqa: E402

from app.database import SessionLocal
from app.models import CodeEntry, CodeSystem

SAMPLES = Path(__file__).resolve().parent.parent / "scripts" / "samples"


def _entry_count(system_code: str) -> int:
    db = SessionLocal()
    try:
        system = db.query(CodeSystem).filter(CodeSystem.code == system_code).first()
        if system is None:
            return 0
        return db.query(CodeEntry).filter(CodeEntry.system_id == system.id).count()
    finally:
        db.close()


def test_startup_seed_icd10_and_drugs(client, admin):
    """启动种子：常用 ICD-10 诊断 100 条 + 常用药品 50 条入库并可检索。"""
    assert _entry_count("diagnosis") == 100
    assert _entry_count("drug") == 50
    hits = client.get("/api/dictionaries/diagnosis/entries?keyword=糖尿病", headers=admin).json()
    assert any(e["code"] == "E11" for e in hits)
    drugs = client.get("/api/dictionaries/drug/entries?keyword=二甲双胍", headers=admin).json()
    assert len(drugs) == 1 and drugs[0]["code"] == "A10BA02"


def test_dry_run_reports_without_writing(client):
    before = _entry_count("diagnosis")
    report = run_import("diagnosis", SAMPLES / "icd10_diagnoses.csv", dry_run=True)
    assert report.dry_run is True
    assert report.imported == 50 and report.skipped == 0 and report.errors == []
    assert _entry_count("diagnosis") == before  # 未落库


def test_import_and_idempotent_rerun(client, admin):
    progress_lines = []
    report = run_import(
        "diagnosis",
        SAMPLES / "icd10_diagnoses.csv",
        progress_every=20,
        out=progress_lines.append,
    )
    assert report.imported == 50 and report.errors == []
    assert _entry_count("diagnosis") == 150
    assert progress_lines and "进度" in progress_lines[0]
    # 幂等重跑：全部跳过
    rerun = run_import("diagnosis", SAMPLES / "icd10_diagnoses.csv")
    assert rerun.imported == 0 and rerun.skipped == 50
    assert _entry_count("diagnosis") == 150
    # 导入后 API 可检索（细目码）
    hits = client.get(
        "/api/dictionaries/diagnosis/entries?keyword=腰椎间盘", headers=admin
    ).json()
    assert any(e["code"] == "M51.2" for e in hits)


def test_import_drug_catalog(client, admin):
    report = run_import("drug", SAMPLES / "drug_catalog.csv")
    assert report.imported == 50 and report.errors == []
    assert _entry_count("drug") == 100
    hits = client.get("/api/dictionaries/drug/entries?keyword=恩格列净", headers=admin).json()
    assert len(hits) == 1 and hits[0]["code"] == "A10BK03"


def test_error_rows_and_bad_args(client, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("code,name\n,缺编码\nX99,\nX98,正常条目\n", encoding="utf-8")
    report = run_import("diagnosis", bad)
    assert report.imported == 1 and len(report.errors) == 2
    assert report.errors[0][0] == 2  # 首个错误在第2行
    # 未知字典类型
    with pytest.raises(ValueError):
        run_import("unknown", bad)
    # 表头缺失
    noheader = tmp_path / "noheader.csv"
    noheader.write_text("编码,名称\nA,B\n", encoding="utf-8")
    with pytest.raises(ValueError):
        run_import("diagnosis", noheader)
