"""存量导入原样落库的文本列按模型列宽校验；写库仍中途失败时，汇总与错误明细照样落出（P2-1094，第三十一批「运维脚本与后台
任务」扫描 C2-5）。

`scripts/import_legacy.py` 原先只有机构名查了长度。患者姓名、员工姓名 / 职称 / 职务、就诊的医生与诊断编码、住院的病区 / 床号 /
医生这些原样落库的列，开发库（SQLite 不管 VARCHAR 多长）照存；生产库（PostgreSQL）批末提交时 StringDataRightTruncation——整批
连同别人的有效行回滚、导入中断，汇总不打印，errors.csv 也不落盘（落盘写在 `except: rollback; raise` 之后）；dry-run 从不提交，
超长行照样算进「将导入」、报「错误: 0 行」。

修后超过模型列宽（`Model.__table__.c[列].type.length`，用例里同样从模型取，不手抄数字）的记错误行、点名哪一列几个字，别的行
照导，dry-run 报出同样的错误行；万一仍有预料之外的写库失败，错误明细与汇总照样落出、点名回滚的是第几行到第几行的那一批，
异常照原样抛出（退出码不变）。SQLite 不管列宽，这里核对的是错误行本身而不是数据库异常；预料之外的失败用 SQLite 触发器模拟。
"""
import csv
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from conftest import reset_database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import import_legacy  # noqa: E402
from import_legacy import run_import  # noqa: E402

from app.database import SessionLocal, engine
from app.models import (
    Admission,
    Bed,
    Employee,
    Encounter,
    Organization,
    Patient,
    Prescription,
    PrescriptionItem,
    User,
    Ward,
)

ORG = "P21094 卫生院"
CARDS = {"甲": "330127199001021094", "乙": "330127199001031094", "丙": "330127199001041094"}
# 中文字符：按字数（PostgreSQL VARCHAR 的口径）而不是字节数算，64 个汉字的医生姓名照导
FILL = "字"


def _width(model, column: str) -> int:
    return model.__table__.c[column].type.length


@pytest.fixture(scope="module", autouse=True)
def world():
    reset_database()
    with SessionLocal() as db:
        db.add(User(username="admin", password_hash="x", role="admin"))   # 经办账号（created_by），不经 app 启动
        db.add(Organization(name=ORG, org_type="township", level="township"))
        for tag, card in CARDS.items():
            db.add(Patient(name=f"P21094 {tag}", id_card=card, ehc_no=f"EHC-P21094-{tag}"))
        db.commit()


def _csv(tmp_path, name: str, header: str, *lines: str) -> Path:
    path = tmp_path / name
    path.write_text(header + "\n" + "".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _errors_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


@contextmanager
def _write_fails_on(table: str, column: str, value: str):
    """这一行写进库时失败（SQLite 触发器 RAISE(ABORT)）：模拟列宽以外、预料之外的写库失败。

    触发器按表各起一个名字、建时带 IF NOT EXISTS：连接池里别的连接缓存着旧的库结构，全量跑到这里时，前一条用例刚删掉的
    同名触发器在它们眼里还在，不带 IF NOT EXISTS 的 CREATE 按缓存直接报「already exists」（带上它才先核对结构版本号）。"""
    name = f"p21094_boom_{table}"
    with engine.begin() as conn:
        conn.execute(text(
            f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE INSERT ON {table} WHEN NEW.{column} = '{value}' "
            "BEGIN SELECT RAISE(ABORT, 'P21094 模拟写库失败'); END"
        ))
    try:
        yield
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TRIGGER IF EXISTS {name}"))


def test_超长的医生姓名与床号记错误行_同批别的行照导_dry_run报出同样的错误行(tmp_path):
    doctor_max, bed_max = _width(Admission, "doctor_name"), _width(Bed, "bed_no")   # 今天是 64 / 16
    long_bed = "床" * (bed_max + 1)
    path = _csv(
        tmp_path, "admissions.csv", "id_card,org_name,ward_name,bed_no,admitted_at,discharged_at,doctor_name",
        f"{CARDS['甲']},{ORG},内科,1,2026-09-01,,张医生",
        f"{CARDS['乙']},{ORG},内科,2,2026-09-01,,{FILL * (doctor_max + 1)}",
        f"{CARDS['丙']},{ORG},内科,{long_bed},2026-09-01,,李医生",
        f"{CARDS['甲']},{ORG},内科,3,2025-03-01,2025-03-10,{FILL * doctor_max}",   # 卡在上限的照导
    )
    expected = [
        (3, f"列超长: doctor_name {doctor_max + 1} 字（上限 {doctor_max} 字）"),
        (4, f"列超长: bed_no {bed_max + 1} 字（上限 {bed_max} 字）"),
    ]

    dry = run_import("admissions", path, dry_run=True)
    assert (dry.imported, dry.errors) == (2, expected)   # 修前 dry-run 报 4 行将导入、0 错误
    assert "错误: 2 行" in dry.summary()
    with SessionLocal() as db:
        assert db.query(Admission).count() == 0   # dry-run 不落库

    errors_csv = tmp_path / "errors.csv"
    rep = run_import("admissions", path, errors_csv=errors_csv)
    assert (rep.imported, rep.errors) == (2, expected)
    with SessionLocal() as db:
        stays = {(p.name, a.status) for a, p in db.query(Admission, Patient).join(Patient, Admission.patient_id == Patient.id)}
        assert stays == {("P21094 甲", "admitted"), ("P21094 甲", "discharged")}
        assert db.query(Bed).filter(Bed.bed_no == long_bed).count() == 0   # 超长床号没有就地建床
    assert [(r["line_no"], r["error"]) for r in _errors_csv(errors_csv)] == [(str(n), msg) for n, msg in expected]


# (实体, 表头, 行模板, CSV 列, 落进的模型, 模型列)：{v} 是被测列的值，{n} 让两行各不相同
CASES = [
    ("organizations", "name,org_type,level,parent_name,address", "P21094 村{n},village,village,,{v}",
     "address", Organization, "address"),
    ("patients", "name,id_card,gender,birth_date,phone", "{v},44010119900101109{n},男,,", "name", Patient, "name"),
    ("employees", "org_name,name,title,position", f"{ORG},{{v}},,", "name", Employee, "name"),
    ("employees", "org_name,name,title,position", f"{ORG},P21094 员工{{n}},{{v}},", "title", Employee, "title"),
    ("employees", "org_name,name,title,position", f"{ORG},P21094 员工{{n}},,{{v}}", "position", Employee, "position"),
    ("encounters", "id_card,org_name,visit_date,encounter_type,doctor_name,diagnosis_code",
     f"{CARDS['甲']},{ORG},2026-07-0{{n}},outpatient,{{v}},", "doctor_name", Encounter, "doctor_name"),
    ("encounters", "id_card,org_name,visit_date,encounter_type,doctor_name,diagnosis_code",
     f"{CARDS['甲']},{ORG},2026-07-0{{n}},outpatient,,{{v}}", "diagnosis_code", Encounter, "diagnosis_code"),
    ("prescriptions", "rx_no,id_card,org_name,rx_date,drug_code,drug_name,daily_dose,days",
     f"RX{{n}},{CARDS['甲']},{ORG},2026-07-0{{n}},{{v}},甲药,1,1", "drug_code", PrescriptionItem, "drug_code"),
    ("admissions", "id_card,org_name,ward_name,bed_no,admitted_at,discharged_at,doctor_name",
     f"{CARDS['甲']},{ORG},{{v}},9,2026-07-0{{n}},2026-07-0{{n}},", "ward_name", Ward, "name"),
    ("admissions", "id_card,org_name,ward_name,bed_no,admitted_at,discharged_at,doctor_name",
     f"{CARDS['甲']},{ORG},内科,{{v}},2026-07-0{{n}},2026-07-0{{n}},", "bed_no", Bed, "bed_no"),
    ("admissions", "id_card,org_name,ward_name,bed_no,admitted_at,discharged_at,doctor_name",
     f"{CARDS['甲']},{ORG},内科,9,2026-07-0{{n}},2026-07-0{{n}},{{v}}", "doctor_name", Admission, "doctor_name"),
]


@pytest.mark.parametrize(("entity", "header", "template", "col", "model", "column"), CASES,
                         ids=[f"{c[0]}.{c[3]}" for c in CASES])
def test_原样落库的文本列_超过模型列宽记错误行_卡在上限的照导(tmp_path, entity, header, template, col, model, column):
    limit = _width(model, column)
    rep = run_import(entity, _csv(
        tmp_path, f"{entity}.csv", header,
        template.format(n=1, v=FILL * (limit + 1)), template.format(n=2, v=FILL * limit)), dry_run=True)
    assert (rep.imported, rep.errors) == (1, [(2, f"列超长: {col} {limit + 1} 字（上限 {limit} 字）")])


def test_写库中途失败_汇总与错误明细照样落出_点名回滚的行号范围_异常照原样抛出(tmp_path, capsys, monkeypatch):
    """每 2 行提交一次：第 2～3 行已提交；第 4 行职称超长记错误行；第 6 行写库失败，第 4～6 行这一批回滚，第 7 行未处理。

    修前：异常直接抛出，汇总不打、errors.csv 不落盘，第 4 行的错误明细跟着丢了。"""
    title_max = _width(Employee, "title")
    path = _csv(
        tmp_path, "employees.csv", "org_name,name,title,position",
        f"{ORG},P21094 甲,,", f"{ORG},P21094 乙,,", f"{ORG},P21094 丙,{FILL * (title_max + 1)},",
        f"{ORG},P21094 丁,,", f"{ORG},P21094 炸,,", f"{ORG},P21094 戊,,",
    )
    monkeypatch.setattr(sys, "argv", ["import_legacy.py", "employees", str(path), "--batch-size", "2",
                                      "--progress-every", "0"])
    with _write_fails_on("employees", "name", "P21094 炸"), pytest.raises(IntegrityError) as caught:
        import_legacy.main()   # 异常照旧抛出命令行：进程退出码与原先一样
    assert "第 4～6 行这一批整批回滚" in caught.value.__notes__[0]

    out = capsys.readouterr().out
    assert "已导入: 2 行" in out   # 计数退回到上次提交时：回滚的丁、炸不算
    assert "中断: 第 4～6 行这一批整批回滚（其中计入导入的 2 行未落库），此前各批已提交；第 6 行之后未处理" in out
    assert "IntegrityError: P21094 模拟写库失败" in out
    assert f"第 4 行: 列超长: title {title_max + 1} 字（上限 {title_max} 字）" in out
    errors_csv = path.with_suffix(".csv.errors.csv")   # 命令行默认的错误明细路径
    assert f"错误行明细已写入: {errors_csv}" in out
    assert [(r["line_no"], r["name"]) for r in _errors_csv(errors_csv)] == [("4", "P21094 丙")]
    with SessionLocal() as db:
        names = {n for (n,) in db.query(Employee.name).filter(Employee.name.like("P21094 %"))}
    assert names == {"P21094 甲", "P21094 乙"}


def test_处方按张提交_回滚范围从下一张处方的首行算起(tmp_path):
    """处方在读到下一张的首行时才提交上一张：那一行还没写进已提交的一批，回滚范围要从它算起，不能从它的下一行算起。"""
    path = _csv(
        tmp_path, "rx.csv", "rx_no,id_card,org_name,rx_date,drug_code,drug_name,daily_dose,days",
        f"RXA,{CARDS['乙']},{ORG},2026-06-01,X01,甲药,1,1",
        f"RXB,{CARDS['乙']},{ORG},2026-06-02,P21094BOOM,乙药,1,1",
    )
    lines: list[str] = []
    with _write_fails_on("prescription_items", "drug_code", "P21094BOOM"), pytest.raises(IntegrityError) as caught:
        run_import("prescriptions", path, batch_size=1, out=lines.append)
    assert "第 3～3 行这一批整批回滚（其中计入导入的 1 行未落库）" in caught.value.__notes__[0]
    assert "已导入: 1 行" in lines[0]
    with SessionLocal() as db:
        pid = db.query(Patient.id).filter(Patient.ehc_no == "EHC-P21094-乙").scalar()
        assert db.query(Prescription).filter(Prescription.patient_id == pid).count() == 1   # RXA 已提交，RXB 连同处方头回滚
