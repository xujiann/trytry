"""导入工具认得出经 Excel 一开一存改坏的标识：床号的前导零、科学计数法的证件号与编码、GBK 另存的文件（P2-1125，第三十二批
「导出文件的格式陷阱」扫描 B3-2）。

`import_legacy` 的 errors.csv 原样回写原始列、docstring 让人「修正后重新导入」——多半是在 Excel 里改。一开一存：
- 床号 `01` 成了 `1`：住院导入按字面找床，找不到就另建一张，与 `01` 并存，「在院不可同床」拦不住（01 床已有人在院，1 床照收）；
- 14 位本位码成了 `8.69E+13`：字典导入原样收下（字典是给外部系统下载对照的）；处方的药品编码、就诊的诊断编码同形；
- 18 位证件号成了 `1.10101E+17`：患者导入报「长度非法」、引用它的导入报「患者不存在」，都不说是表格软件改的；
- Excel「CSV（逗号分隔）」默认另存成 GBK：读到第一个汉字就抛 `'utf-8' codec can't decode byte`，不说该怎么办。
修后前两类记错误行、点名哪一列（床号点名已有的写法，不另建床），GBK 文件开读之前就报「不是 UTF-8」、命令行退出码 2。
被抹成 …000 的 18 位证件号认不出（要核校验位，见 P2-811）；errors.csv 的标识列怎么写才不被 Excel 改，见待裁定。
"""
import sys
from pathlib import Path

import pytest

from conftest import reset_database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import import_dictionary  # noqa: E402
import import_legacy  # noqa: E402
from import_legacy import run_import  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Admission, Bed, CodeEntry, Organization, Patient, Prescription, User, Ward  # noqa: E402
from app.texttypes import excel_sci_notation  # noqa: E402

ORG = "P21125 卫生院"
CARDS = {"甲": "330127199001021125", "乙": "330127199001031125", "丙": "330127199001041125"}


@pytest.fixture(scope="module", autouse=True)
def world():
    reset_database()
    with SessionLocal() as db:
        db.add(User(username="admin", password_hash="x", role="admin"))   # 经办账号（created_by），不经 app 启动
        db.add(Organization(name=ORG, org_type="township", level="township"))
        for tag, card in CARDS.items():
            db.add(Patient(name=f"P21125 {tag}", id_card=card, ehc_no=f"EHC-P21125-{tag}"))
        db.commit()


def _csv(tmp_path, name: str, header: str, *lines: str, encoding: str = "utf-8") -> Path:
    path = tmp_path / name
    path.write_text(header + "\n" + "".join(f"{line}\n" for line in lines), encoding=encoding)
    return path


ADMISSION_HEADER = "id_card,org_name,ward_name,bed_no,admitted_at,discharged_at,doctor_name"


def _beds(ward_name: str) -> list[str]:
    with SessionLocal() as db:
        return sorted(no for (no,) in db.query(Bed.bed_no).join(Ward, Bed.ward_id == Ward.id).filter(Ward.name == ward_name))


def test_床号吃掉前导零_不另建一张床_点名已有的写法(tmp_path):
    first = run_import("admissions", _csv(tmp_path, "a1.csv", ADMISSION_HEADER,
                                           f"{CARDS['甲']},{ORG},呼吸内科,01,2026-09-01,,张医生"))
    assert (first.imported, first.errors) == (1, [])
    # 乙那一行进过 errors.csv、在 Excel 里改过再存：01 成了 1
    rep = run_import("admissions", _csv(tmp_path, "a2.csv", ADMISSION_HEADER,
                                         f"{CARDS['乙']},{ORG},呼吸内科,1,2026-09-02,,李医生",
                                         f"{CARDS['丙']},{ORG},呼吸内科,02,2026-09-02,,王医生"))
    assert rep.imported == 1   # 02 是另一张床，照建照导
    assert rep.errors == [(2, "床号 1 与本病区已有的床号 01 数值相同、疑似同一张床（表格软件会吃掉前导零）：请按已有写法填")]
    assert _beds("呼吸内科") == ["01", "02"]   # 修前多出一张「1」床，乙与甲同一张床都在院
    with SessionLocal() as db:
        assert db.query(Admission).filter(Admission.status == "admitted").count() == 2


def test_已有的是不带零的写法_带零的也认得(tmp_path):
    run_import("admissions", _csv(tmp_path, "b1.csv", ADMISSION_HEADER,
                                   f"{CARDS['甲']},{ORG},心内科,3,2025-01-01,2025-01-05,张医生"))
    rep = run_import("admissions", _csv(tmp_path, "b2.csv", ADMISSION_HEADER,
                                         f"{CARDS['甲']},{ORG},心内科,003,2025-02-01,2025-02-03,张医生"))
    assert rep.imported == 0 and "与本病区已有的床号 3 数值相同" in rep.errors[0][1]
    assert _beds("心内科") == ["3"]


def test_科学计数法的证件号_点名是表格软件改的(tmp_path):
    rep = run_import("patients", _csv(tmp_path, "p.csv", "name,id_card,gender,birth_date,phone",
                                       "P21125 丁,1.10101E+17,男,,"), dry_run=True)
    assert rep.errors == [(2, "疑似被表格软件改成科学计数法: id_card 1.10101E+17（把这一列设成文本格式、改回原值后重导）")]   # 修前「长度非法」
    enc = run_import("encounters", _csv(tmp_path, "e.csv", "id_card,org_name,visit_date,encounter_type,doctor_name,diagnosis_code",
                                         f"3.30127E+17,{ORG},2026-07-01,outpatient,,",
                                         f"{CARDS['甲']},{ORG},2026-07-02,outpatient,,1.23E+5"), dry_run=True)
    assert [reason for _, reason in enc.errors] == [   # 修前：患者不存在；诊断编码原样落库
        "疑似被表格软件改成科学计数法: id_card 3.30127E+17（把这一列设成文本格式、改回原值后重导）",
        "疑似被表格软件改成科学计数法: diagnosis_code 1.23E+5（把这一列设成文本格式、改回原值后重导）",
    ]


def test_处方的药品编码是科学计数法_整张不导(tmp_path):
    rep = run_import("prescriptions", _csv(
        tmp_path, "rx.csv", "rx_no,id_card,org_name,rx_date,drug_code,drug_name,daily_dose,days",
        f"RX1125,{CARDS['甲']},{ORG},2026-07-01,X01,甲药,1,1",
        f"RX1125,{CARDS['甲']},{ORG},2026-07-01,8.69E+13,乙药,1,1"))
    assert rep.imported == 0 and "drug_code 8.69E+13" in rep.errors[0][1]
    with SessionLocal() as db:
        assert db.query(Prescription).count() == 0


def test_字典的本位码是科学计数法_记错误行_好行照导(tmp_path):
    path = _csv(tmp_path, "drug.csv", "code,name,spec,dosage_form,manufacturer,unit,insurance_code,national_code,extra",
                "ZZ1125A,二甲双胍片,,,,,,86900000000011,", "ZZ1125B,恩格列净片,,,,,,8.69E+13,")
    rep = import_dictionary.run_import("drug", path, out=lambda _line: None)
    assert rep.imported == 1
    assert rep.errors == [(3, "疑似被表格软件改成科学计数法：national_code 8.69E+13（把这一列设成文本格式、改回原值后重导）")]
    with SessionLocal() as db:
        assert {c for (c,) in db.query(CodeEntry.code).filter(CodeEntry.code.like("ZZ1125%"))} == {"ZZ1125A"}


def test_GBK另存的文件_开读之前就说清_命令行退出码2(tmp_path, monkeypatch, capsys):
    path = _csv(tmp_path, "gbk.csv", "name,id_card,gender,birth_date,phone", f"P21125 戊,{CARDS['丙']}9,男,,",
                encoding="gbk")
    with pytest.raises(ValueError, match="不是 UTF-8 编码"):
        run_import("patients", path)
    monkeypatch.setattr(sys, "argv", ["import_legacy.py", "patients", str(path)])
    assert import_legacy.main() == 2   # 修前 UnicodeDecodeError 带栈退出
    assert "另存为「CSV UTF-8（逗号分隔）」" in capsys.readouterr().err
    with SessionLocal() as db:
        assert db.query(Patient).filter(Patient.name == "P21125 戊").count() == 0


@pytest.mark.parametrize("value, expected", [
    ("8.69E+13", True), ("1.10101E+17", True), ("3e+5", True), (" 8.69E+13 ", True),
    ("86900000000011", False), ("E11", False), ("1E5", False), ("XA10BA02A001010100001", False), ("", False), (None, False),
])
def test_科学计数法的认法只认表格软件那种写法(value, expected):
    assert excel_sci_notation(value) is expected
