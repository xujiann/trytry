"""存量住院导入：已在院的患者再来一条在院行，记进错误行，不连累别人的有效行（P2-583，第十二批扫描 Z4-8）。

平台入院登记对「该患者已在院」409，兜底是部分唯一索引 `uq_admission_patient_admitted`；导入脚本只判了「同床两条
在院」，没判「同一患者两条在院」——撞上唯一索引，整批（连同同一批里别人的有效行）回滚，错误明细文件不落盘。
"""
import sys
from pathlib import Path

import pytest

from conftest import reset_database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_legacy import run_import  # noqa: E402

from app.database import SessionLocal
from app.models import Admission, Organization, Patient, User

HEADER = "id_card,org_name,ward_name,bed_no,admitted_at,discharged_at,diagnosis_name\n"
CARDS = {"甲": "330127199001025830", "乙": "330127199001025831", "丙": "330127199001025832"}


@pytest.fixture(scope="module", autouse=True)
def world():
    reset_database()
    with SessionLocal() as db:
        db.add(User(username="admin", password_hash="x", role="admin"))   # 经办账号（created_by），不经 app 启动
        db.add(Organization(name="P2583 卫生院", org_type="township", level="township"))
        for tag, card in CARDS.items():
            db.add(Patient(name=f"P2583 {tag}", id_card=card, ehc_no=f"EHC-P2583-{tag}"))
        db.commit()


def _csv(tmp_path, name: str, *lines: str) -> Path:
    path = tmp_path / name
    path.write_text(HEADER + "".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _stays(tag: str) -> list[str]:
    with SessionLocal() as db:
        pid = db.query(Patient.id).filter(Patient.id_card == CARDS[tag]).scalar()
        return [a.status for a in db.query(Admission).filter(Admission.patient_id == pid).order_by(Admission.id)]


def test_已在院的患者再来一条在院行_记错误行_同批别人的照常导入(tmp_path):
    first = run_import("admissions", _csv(tmp_path, "a.csv", f"{CARDS['甲']},P2583 卫生院,内科,1,2026-09-20,,肺炎"))
    assert first.imported == 1 and first.errors == []
    rep = run_import("admissions", _csv(
        tmp_path, "b.csv",
        f"{CARDS['乙']},P2583 卫生院,内科,3,2026-09-20,,高血压",
        f"{CARDS['甲']},P2583 卫生院,内科,2,2026-09-21,,高血压"))   # 甲已在 1 床
    assert rep.imported == 1, rep.errors   # 修前撞唯一索引，整批回滚、异常抛出
    assert [(line, "已有在院记录" in msg) for line, msg, *_ in rep.errors] == [(3, True)]
    assert _stays("甲") == ["admitted"] and _stays("乙") == ["admitted"]


def test_同一批里同一患者两条在院行_第二条记错误行(tmp_path):
    rep = run_import("admissions", _csv(
        tmp_path, "c.csv",
        f"{CARDS['丙']},P2583 卫生院,内科,5,2026-09-22,,肺炎",
        f"{CARDS['丙']},P2583 卫生院,外科,1,2026-09-23,,骨折"))
    assert rep.imported == 1 and len(rep.errors) == 1 and rep.errors[0][0] == 3, rep.errors
    assert _stays("丙") == ["admitted"]


def test_已在院的患者照样能补导历史出院记录(tmp_path):
    rep = run_import("admissions", _csv(tmp_path, "d.csv", f"{CARDS['甲']},P2583 卫生院,内科,1,2025-03-01,2025-03-10,胃炎"))
    assert rep.imported == 1 and rep.errors == [], rep.errors   # 出院的不占床、不算在院
    assert sorted(_stays("甲")) == ["admitted", "discharged"]
