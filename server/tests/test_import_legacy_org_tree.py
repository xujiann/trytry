"""存量导入补机构树：同名机构与文件不一致记错误行，新建的按层级阶梯校验上级（P2-1342，第三十九批扫描 AC3-6）。

修前：`scripts/import_legacy.py` 的机构导入对库里已有的同名机构一律幂等跳过——上级 / 层级 / 类型与文件不一致也照样记「幂等
跳过(已存在)」、退出码 0；上级解析只在新建时用。ADR-0004 把「没跑 import_legacy」列为机构树未配置的原因，改好文件重导是运维
最自然的补救，结果是空操作：文件写「西村→甲镇」「南村 village→甲镇」，导入报已导入 0 / 跳过 4 / 错误 0，导入后西村仍是孤儿、
南村仍是乡级挂在县医院下。新建机构给了上级也不看层级，村挂县、乡挂乡照导（与建机构接口同一个缺口，P1-247）。

修后：同名机构的上级 / 层级 / 类型与文件不一致记错误行，点名库内值与文件值、注明平台不支持改上级（P2-441），退出码照「有错误
行即 1」；完全一致的照旧幂等跳过。新建机构给了上级的按 `organizations.parent_level_problem` 校验（与建机构接口同一句），错挂的记
错误行。造数照 `scan39/ac3/r7_import_skip_parent.py`。
"""
import sys
from pathlib import Path

import pytest

from conftest import reset_database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import import_legacy  # noqa: E402

from app.database import SessionLocal
from app.models import Organization

HEADER = "name,org_type,level,parent_name,address"


@pytest.fixture(autouse=True)
def world():
    """县人民医院 → 甲镇卫生院 合法；西村卫生室页面漏选上级（孤儿）；南村卫生室层级选成乡级、挂在县医院下。"""
    reset_database()
    with SessionLocal() as db:
        county = Organization(name="县人民医院", org_type="lead_hospital", level="county")
        db.add(county)
        db.flush()
        town = Organization(name="甲镇卫生院", org_type="township", level="township", parent_id=county.id)
        db.add_all([town, Organization(name="西村卫生室", org_type="village", level="village"),
                    Organization(name="南村卫生室", org_type="village", level="township", parent_id=county.id)])
        db.commit()


def _run(tmp_path, monkeypatch, *lines: str) -> int:
    path = tmp_path / "orgs.csv"
    path.write_text(HEADER + "\n" + "".join(f"{line}\n" for line in lines), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["import_legacy.py", "organizations", str(path)])
    return import_legacy.main()


def _tree() -> dict[str, tuple[str, str, str | None]]:
    with SessionLocal() as db:
        names = {o.id: o.name for o in db.query(Organization)}
        return {o.name: (o.org_type, o.level, names.get(o.parent_id)) for o in db.query(Organization)}


def test_同名机构与文件不一致_记错误行_退出码1(tmp_path, monkeypatch, capsys):
    before = _tree()
    code = _run(tmp_path, monkeypatch,
                "县人民医院,lead_hospital,county,,",
                "甲镇卫生院,township,township,县人民医院,",
                "西村卫生室,village,village,甲镇卫生院,",
                "南村卫生室,village,village,甲镇卫生院,")
    out = capsys.readouterr().out
    assert code == 1, out   # 修前 0
    assert "幂等跳过(已存在): 2 行" in out and "错误: 2 行" in out, out   # 修前 跳过 4 / 错误 0
    assert "第 4 行: 机构已存在、与文件不一致: parent_name 库内为「」、文件为「甲镇卫生院」" in out, out
    assert ("第 5 行: 机构已存在、与文件不一致: level 库内为「township」、文件为「village」；"
            "parent_name 库内为「县人民医院」、文件为「甲镇卫生院」") in out, out
    assert out.count("平台不支持改上级，见 P2-441") == 2, out
    assert _tree() == before   # 库里一行没动


def test_同名机构与文件完全一致_照旧幂等跳过_退出码0(tmp_path, monkeypatch, capsys):
    code = _run(tmp_path, monkeypatch,
                "县人民医院,lead_hospital,county,,",
                "甲镇卫生院,township,township,县人民医院,",
                "西村卫生室,village,village,,",
                "南村卫生室,village,township,县人民医院,")
    out = capsys.readouterr().out
    assert code == 0, out
    assert "幂等跳过(已存在): 4 行" in out and "错误: 0 行" in out, out


def test_同名机构的类型不一致也记错误行(tmp_path, monkeypatch, capsys):
    assert _run(tmp_path, monkeypatch, "县人民医院,public_health,county,,") == 1
    assert "org_type 库内为「lead_hospital」、文件为「public_health」" in capsys.readouterr().out


def test_新建机构给了上级_错挂的记错误行(tmp_path, monkeypatch, capsys):
    code = _run(tmp_path, monkeypatch,
                "北村卫生室,village,village,县人民医院,",    # 村挂县
                "东村卫生室,village,village,甲镇卫生院,",    # 合法
                "中村卫生室,village,village,东村卫生室,",    # 村挂村（上级是同一文件里刚建的）
                "乙镇卫生院,township,township,甲镇卫生院,",  # 乡挂乡
                "丙镇卫生院,township,township,县人民医院,")  # 合法
    out = capsys.readouterr().out
    assert code == 1, out   # 修前 0，四家全导
    assert "已导入: 2 行" in out and "错误: 3 行" in out, out
    assert "第 2 行: 村级机构的上级只能是乡级机构，所选上级「县人民医院」是县级" in out, out
    assert "第 4 行: 村级机构的上级只能是乡级机构，所选上级「东村卫生室」是村级" in out, out
    assert "第 5 行: 乡级机构的上级只能是市级或县级机构，所选上级「甲镇卫生院」是乡级" in out, out
    tree = _tree()
    assert tree["东村卫生室"][2] == "甲镇卫生院" and tree["丙镇卫生院"][2] == "县人民医院"
    assert not {"北村卫生室", "中村卫生室", "乙镇卫生院"} & set(tree)
