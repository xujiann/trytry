"""D1 字典扩列：CodeEntry 属性列（规格/剂型/厂家/单位/医保对码/本位码/extra）。

覆盖：含新列 CSV 导入与幂等重跑、旧版缺列 CSV 兼容（不填不报错）、
dry-run 不落库、API 入参新列可选 + 响应契约含新字段。
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_dictionary import run_import  # noqa: E402

from app.database import SessionLocal
from app.main import app
from app.models import CodeEntry, CodeSystem

SAMPLES = Path(__file__).resolve().parent.parent / "scripts" / "samples"


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _entry(system_code: str, code: str) -> CodeEntry | None:
    db = SessionLocal()
    try:
        system = db.query(CodeSystem).filter(CodeSystem.code == system_code).first()
        if system is None:
            return None
        return (
            db.query(CodeEntry)
            .filter(CodeEntry.system_id == system.id, CodeEntry.code == code)
            .first()
        )
    finally:
        db.close()


def test_import_with_extended_columns_and_idempotent_rerun(client):
    report = run_import("drug", SAMPLES / "drug_catalog_full.csv")
    # A10BA02/A10BK03 已在启动种子里（幂等跳过不覆盖），C08CA06 为新增
    assert report.errors == []
    assert report.imported + report.skipped == 3

    entry = _entry("drug", "C08CA06")
    assert entry is not None
    assert entry.spec == "30mg×20片"
    assert entry.dosage_form == "片剂"
    assert entry.manufacturer == "示例医药集团"
    assert entry.unit == "盒"
    assert entry.insurance_code is None  # 样例该行医保对码留空 → 不填
    assert entry.national_code == "86900000000035"

    # 幂等重跑：全部跳过、不覆盖、不新增
    rerun = run_import("drug", SAMPLES / "drug_catalog_full.csv")
    assert rerun.imported == 0 and rerun.skipped == 3 and rerun.errors == []


def test_legacy_csv_without_new_columns_still_works(client, tmp_path):
    """缺列兼容：旧版 code,name 两列 CSV 照常导入，新列保持 NULL。"""
    legacy = tmp_path / "legacy.csv"
    legacy.write_text("code,name\nZZLEG01,缺列兼容药\n", encoding="utf-8")
    report = run_import("drug", legacy)
    assert report.imported == 1 and report.errors == []
    entry = _entry("drug", "ZZLEG01")
    assert entry is not None and entry.name == "缺列兼容药"
    assert entry.spec is None and entry.extra is None


def test_dry_run_with_new_columns_does_not_write(client, tmp_path):
    csv_file = tmp_path / "dry.csv"
    csv_file.write_text(
        "code,name,spec,manufacturer\nZZDRY01,校验药,1g×10支,某厂\n", encoding="utf-8"
    )
    report = run_import("drug", csv_file, dry_run=True)
    assert report.dry_run is True and report.imported == 1
    assert _entry("drug", "ZZDRY01") is None


def test_api_create_and_list_include_new_fields(client, admin):
    created = client.post(
        "/api/dictionaries/consumable/entries",
        json={
            "code": "HC0001",
            "name": "一次性注射器",
            "spec": "5ml",
            "unit": "支",
            "insurance_code": "C0001XXX",
        },
        headers=admin,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["spec"] == "5ml" and body["unit"] == "支"
    assert body["insurance_code"] == "C0001XXX"
    assert body["dosage_form"] is None  # 未填字段输出 null（response_model 一一对应）

    # 旧口径调用（不带新列）仍可创建；列表响应含全部新字段键
    ok = client.post(
        "/api/dictionaries/consumable/entries",
        json={"code": "HC0002", "name": "医用棉签"},
        headers=admin,
    )
    assert ok.status_code == 201
    rows = client.get("/api/dictionaries/consumable/entries", headers=admin).json()
    assert {"spec", "dosage_form", "manufacturer", "unit",
            "insurance_code", "national_code", "extra"} <= set(rows[0].keys())


def test_api_bulk_import_accepts_new_fields(client, admin):
    resp = client.post(
        "/api/dictionaries/consumable/import",
        json=[
            {"code": "HC1001", "name": "留置针", "spec": "24G", "manufacturer": "某厂"},
            {"code": "HC1001", "name": "留置针"},  # 同批重复 → 跳过
        ],
        headers=admin,
    )
    assert resp.status_code == 200
    assert resp.json() == {"imported": 1, "skipped": 1}  # 响应契约保持两键
    entry = _entry("consumable", "HC1001")
    assert entry is not None and entry.spec == "24G"
