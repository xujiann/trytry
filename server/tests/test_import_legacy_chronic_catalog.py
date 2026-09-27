"""存量慢病档案导入与平台建档同一个真源（P2-584，第十二批「批量 vs 单条」扫描 Z4-7）。

平台建档按慢病病种目录校验病种（停用的也拒）、到期日留空按病种随访周期建议首次随访；导入脚本原先按一份手抄的
五个病种校验——收了目录里没有的肥胖、高血脂，拒了目录里有的冠心病、脑卒中、严重精神障碍、肿瘤、结核——到期日
留空就空着，导进来的人从此不进任何到期清单。
"""
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import reset_database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_legacy import run_import  # noqa: E402

from app import clock
from app.chronic_seed import SEED_CHRONIC_DISEASE_TYPES
from app.database import SessionLocal
from app.models import ChronicDiseaseType, ChronicPatient, Organization, Patient

CARD = "330127195501015840"
HEADER = "id_card,disease,level,managed_by_org,next_due\n"


@pytest.fixture(scope="module", autouse=True)
def world():
    reset_database()
    with SessionLocal() as db:
        db.add(Organization(name="P2584 卫生院", org_type="township", level="township"))
        db.add(Patient(name="P2584 患者", id_card=CARD, ehc_no="EHC-P2584"))
        db.add_all(ChronicDiseaseType(**seed) for seed in SEED_CHRONIC_DISEASE_TYPES)
        db.commit()
        db.query(ChronicDiseaseType).filter(ChronicDiseaseType.code == "tuberculosis").update({"active": False})
        db.commit()


def _import(tmp_path, *lines: str):
    path = tmp_path / "chronic.csv"
    path.write_text(HEADER + "".join(f"{line}\n" for line in lines), encoding="utf-8")
    return run_import("chronic", path)


def _record(disease: str) -> ChronicPatient | None:
    with SessionLocal() as db:
        return db.query(ChronicPatient).filter(ChronicPatient.disease == disease).one_or_none()


def test_目录里有的病种照常导入_到期日留空按病种周期建议(tmp_path):
    rep = _import(tmp_path, f"{CARD},stroke,2,P2584 卫生院,")
    assert rep.imported == 1 and rep.errors == [], rep.errors   # 修前「disease 非法」：手抄表里没有脑卒中
    with SessionLocal() as db:
        interval = db.query(ChronicDiseaseType.followup_interval_days).filter(
            ChronicDiseaseType.code == "stroke").scalar()
    assert _record("stroke").next_due == (clock.today() + timedelta(days=interval)).isoformat()   # 修前空串


def test_给了到期日照原样(tmp_path):
    rep = _import(tmp_path, f"{CARD},chd,1,P2584 卫生院,2026-12-01")
    assert rep.imported == 1 and _record("chd").next_due == "2026-12-01"


@pytest.mark.parametrize("disease", ["obesity", "tuberculosis"])
def test_目录里没有的或已停用的病种记错误行(tmp_path, disease):
    rep = _import(tmp_path, f"{CARD},{disease},1,P2584 卫生院,")
    assert rep.imported == 0 and len(rep.errors) == 1 and "病种目录" in rep.errors[0][1], rep.errors   # 修前 obesity 导得进
    assert _record(disease) is None
