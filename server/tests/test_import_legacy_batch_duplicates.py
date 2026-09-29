"""存量导入的幂等只认库里已有的：同一文件里撞键的后一行记错误行，不再计「已存在」（P2-732，第十九批「导入 / 入站对接
vs 界面录入」扫描 K3-10）。

各实体原先把本批刚导的键也塞进「库内已存在」那个集合：同日上午门诊 I10、当天收住院 I10，住院那条被跳过（就诊键不含
就诊类型）；同日两笔 12 元挂号结算、同机构两位「王芳」，后一条库里原本没有，却被计成「幂等跳过(已存在)」悄悄丢掉。
同文件的患者导入早就写明「同批内重复单独报错（与库内已存在的幂等跳过语义区分，便于清洗源文件）」。修法：就诊键补上
就诊类型；各实体同一文件里撞键的后一行记错误行、点名与第几行相同；库里真有的照旧幂等跳过。按原系统流水号判重另行裁定。
"""
import sys
from pathlib import Path

import pytest

from conftest import reset_database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_legacy import run_import  # noqa: E402

from app.database import SessionLocal
from app.models import Encounter, Organization, Patient, User

CARD = "330127199001027320"
ORG = "P2732 卫生院"


@pytest.fixture(scope="module", autouse=True)
def world():
    reset_database()
    with SessionLocal() as db:
        db.add(User(username="admin", password_hash="x", role="admin"))   # 经办账号（created_by），不经 app 启动
        db.add(Organization(name=ORG, org_type="township", level="township"))
        db.add(Patient(name="P2732 患者", id_card=CARD, ehc_no="EHC-P2732"))
        db.commit()


def _csv(tmp_path, name: str, header: str, *lines: str) -> Path:
    path = tmp_path / name
    path.write_text(header + "\n" + "".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _count(model, **filters) -> int:
    with SessionLocal() as db:
        return db.query(model).filter_by(**filters).count()


def test_同日门诊与住院_两条都导入(tmp_path):
    rep = run_import("encounters", _csv(
        tmp_path, "e.csv", "id_card,org_name,visit_date,encounter_type,diagnosis_code,diagnosis_name",
        f"{CARD},{ORG},2026-03-01,outpatient,I10,高血压", f"{CARD},{ORG},2026-03-01,inpatient,I10,高血压"))
    assert (rep.imported, rep.skipped, rep.errors) == (2, 0, [])   # 修前 1 / 1：住院那条当成已存在
    assert _count(Encounter, encounter_type="inpatient") == 1


@pytest.mark.parametrize(("entity", "header", "line"), [
    ("settlements", "id_card,org_name,bill_type,settle_date,total_amount", f"{CARD},{ORG},outpatient,2026-03-02,12"),
    ("employees", "org_name,name,title,position", f"{ORG},王芳,主治医师,内科"),
    ("encounters", "id_card,org_name,visit_date,encounter_type,diagnosis_code", f"{CARD},{ORG},2026-03-03,outpatient,"),
], ids=["同日两笔同额挂号", "同机构两位同名员工", "同日两次就诊诊断编码留空"])
def test_同一文件撞键的后一行记错误行_不计已存在(tmp_path, entity, header, line):
    rep = run_import(entity, _csv(tmp_path, f"{entity}.csv", header, line, line))
    assert (rep.imported, rep.skipped) == (1, 0), (rep.imported, rep.skipped, rep.errors)   # 修前 1 / 1
    assert [(n, "同批内重复" in msg and "第 2 行" in msg) for n, msg in rep.errors] == [(3, True)], rep.errors
    # 库里真有的照旧幂等跳过：这一行再导一遍是「已存在」
    again = run_import(entity, _csv(tmp_path, f"{entity}-again.csv", header, line))
    assert (again.imported, again.skipped, again.errors) == (0, 1, [])
