"""D1 收费项目批量导入（scripts/import_charge_items.py）：幂等 code 查重、
Money 口径价格校验、charge 字典管控口径与接口一致。"""
import sys
from pathlib import Path

import pytest

from conftest import reset_database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_charge_items import run_import  # noqa: E402

from app.database import SessionLocal
from app.models import ChargeItem, CodeEntry, CodeSystem

SAMPLES = Path(__file__).resolve().parent.parent / "scripts" / "samples"


@pytest.fixture(scope="module", autouse=True)
def fresh_db():
    reset_database()
    yield


def _count() -> int:
    db = SessionLocal()
    try:
        return db.query(ChargeItem).count()
    finally:
        db.close()


def test_import_and_idempotent_rerun():
    report = run_import(SAMPLES / "charge_items.csv")
    assert report.imported == 3 and report.errors == []
    db = SessionLocal()
    try:
        item = db.query(ChargeItem).filter(ChargeItem.code == "CJ001").one()
        assert item.name == "血常规" and item.category == "exam" and item.price == 22.50
    finally:
        db.close()
    # 幂等：code 查重全跳过，不覆盖价格（调价走调价接口留痕）
    rerun = run_import(SAMPLES / "charge_items.csv")
    assert rerun.imported == 0 and rerun.skipped == 3
    assert _count() == 3


def test_price_money_validation(tmp_path):
    bad = tmp_path / "bad_price.csv"
    bad.write_text(
        "code,name,category,price,active\n"
        "BAD01,零价项目,other,0,true\n"
        "BAD02,负价项目,other,-1,true\n"
        "BAD03,三位小数,other,1.005,true\n"
        "BAD04,非数值,other,abc,true\n"
        "OK001,正常项目,other,5.50,true\n",
        encoding="utf-8",
    )
    rep = run_import(bad)
    assert rep.imported == 1 and len(rep.errors) == 4


def test_dry_run_does_not_write(tmp_path):
    before = _count()
    f = tmp_path / "dry.csv"
    f.write_text("code,name,category,price,active\nDRY01,校验项目,other,9.90,true\n", encoding="utf-8")
    rep = run_import(f, dry_run=True)
    assert rep.dry_run is True and rep.imported == 1
    assert _count() == before


def test_charge_dict_control(tmp_path):
    """charge 字典已配置条目时，仅字典内编码可入目录（与接口同口径）。"""
    db = SessionLocal()
    try:
        system = db.query(CodeSystem).filter(CodeSystem.code == "charge").first()
        if system is None:
            system = CodeSystem(code="charge", name="收费")
            db.add(system)
            db.flush()
        db.add(CodeEntry(system_id=system.id, code="INDICT01", name="字典内项目"))
        db.commit()
    finally:
        db.close()
    f = tmp_path / "dict_control.csv"
    f.write_text(
        "code,name,category,price,active\n"
        "INDICT01,字典内项目,other,3.00,true\n"
        "OUTDICT9,字典外项目,other,3.00,true\n",
        encoding="utf-8",
    )
    rep = run_import(f)
    assert rep.imported == 1 and len(rep.errors) == 1
    assert "不在收费字典" in rep.errors[0][1]
