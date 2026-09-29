"""修复前导入的慢病档案到期日是空串：同一份 CSV 重导时补上（P2-888，第二十四批「存量行 vs 新规则」扫描 Z3-1）。

P2-584 让导入时到期日留空按病种随访周期建议，但修复之前导进来的行原样是空串：超期名单、到期扫描都按 `next_due != ""`
过滤，这些人从此不进任何到期清单；拿同一份 CSV 重导，按（患者, 病种）幂等跳过，到期日还是空。慢病档案没有改档接口，
只有录一次随访才会写到期日。修后重导遇到「已存在且到期日为空」的行只补空值（与新行同一个取法），报告单列一行；已有
到期日的照旧幂等跳过。
"""
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_legacy import run_import  # noqa: E402

from app import clock
from app.database import SessionLocal
from app.models import ChronicDiseaseType, ChronicPatient, Organization, Patient

CARDS = ("330127195501012881", "330127195601012889")
HEADER = "id_card,disease,level,managed_by_org,next_due\n"


@pytest.fixture(scope="module", autouse=True)
def world(client):   # 跟在 client 之后：它先重建库表、种好病种目录
    with SessionLocal() as db:
        org = Organization(name="P2888 卫生院", org_type="township", level="township")
        db.add(org)
        patients = [Patient(name=f"P2888 患者{n}", id_card=card, ehc_no=f"EHC-P2888-{n}") for n, card in enumerate(CARDS)]
        db.add_all(patients)
        db.commit()
        db.add(ChronicPatient(patient_id=patients[0].id, disease="hypertension", level=1, managed_by_org_id=org.id,
                              next_due=""))
        db.commit()   # 修复前导入的形状：到期日空串


def _import(tmp_path, dry_run=False):
    path = tmp_path / "chronic.csv"
    path.write_text(HEADER + "".join(f"{card},hypertension,1,P2888 卫生院,\n" for card in CARDS), encoding="utf-8")
    return run_import("chronic", path, dry_run=dry_run)


def _dues():
    with SessionLocal() as db:
        return [due for (due,) in db.query(ChronicPatient.next_due).order_by(ChronicPatient.patient_id)]


def test_重导补上空到期日_进超期名单(client, admin, tmp_path):
    preview = _import(tmp_path, dry_run=True)
    assert (preview.imported, preview.filled, preview.skipped) == (1, 1, 0), preview.summary()
    assert "将补到期日(已存在、原先为空): 1 行" in preview.summary()
    assert _dues() == [""]   # 校验模式不落库

    rep = _import(tmp_path)
    assert (rep.imported, rep.filled, rep.skipped, rep.errors) == (1, 1, 0, []), rep.summary()   # 修前 (1, -, 1)：甲照旧空着
    with SessionLocal() as db:
        interval = db.query(ChronicDiseaseType.followup_interval_days).filter(
            ChronicDiseaseType.code == "hypertension").scalar()
    due = (clock.today() + timedelta(days=interval)).isoformat()
    assert _dues() == [due, due]
    later = (clock.today() + timedelta(days=interval + 1)).isoformat()
    overdue = client.get("/api/chronic/overdue", headers=admin, params={"today": later}).json()
    assert len(overdue) == 2   # 修前只有乙

    again = _import(tmp_path)
    assert (again.imported, again.filled, again.skipped) == (0, 0, 2)   # 已有到期日的照旧幂等跳过
    assert "到期日" not in again.summary()
