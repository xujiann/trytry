"""用户手册补上「证明与体检」一页：医师、公卫、管理层三章原先一句没写（§13 把「有手册条目」算进模块功能完整）。

这一页有出生 / 死亡医学证明与出生缺陷儿登记的签发、死因报告卡与批量导出、成人健康体检登记与总检，三类角色各管一段：
签发与体检登记收医师、公卫（`certs.issue_cert` / `checkups.create_checkup`），总检只收医师（`checkups.review_checkup`），
死因报告卡与导出只收管理层。这里钉住手册写的分工就是接口收的角色、手册引的报错原文就是接口回的那句——接口改了，
这里提醒把手册一起改。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANUAL = (ROOT / "docs" / "用户手册.md").read_text(encoding="utf-8")
ROUTERS = ROOT / "server" / "app" / "routers"
CERTS = (ROUTERS / "certs.py").read_text(encoding="utf-8")
CHECKUPS = (ROUTERS / "checkups.py").read_text(encoding="utf-8")


def _chapter(title: str) -> str:
    start = MANUAL.index(title)
    return MANUAL[start:MANUAL.index("\n## ", start + 1)]


def _item(chapter: str, head: str) -> str:
    start = chapter.index(head)
    nxt = re.search(r"\n\d+\. \*\*|\n---", chapter[start + len(head):])
    return chapter[start:start + len(head) + (nxt.start() if nxt else len(chapter))]


def _roles(source: str, func: str) -> str:
    """`def func(` 上面那段装饰器里的 `require_roles(...)` 参数。"""
    head = source[:source.index(f"def {func}(")]
    return re.findall(r"require_roles\(([^)]*)\)", head[head.rindex("@router."):])[0]


def _doctor_step() -> str:
    chapter = _chapter("## 第三章 医师（doctor）")
    assert "17. **证明与体检**" in chapter, "医师一章没写证明与体检（修前三章一句没写）"
    return _item(chapter, "17. **证明与体检**")


def test_医师一章列上这一页并写清签发与体检():
    doctor = _chapter("## 第三章 医师（doctor）")
    assert "证明与体检" in doctor[doctor.index("### 页面清单"):doctor.index("### 关键操作")]
    step = _doctor_step()
    for said, code in (("死亡证明须填患者 ID（关联档案）", '"死亡医学证明须关联患者档案"'),
                       ("并填死因诊断", '"死亡医学证明须填写死因诊断"'),
                       ("缺陷登记须填缺陷诊断", '"出生缺陷儿登记须填写缺陷诊断"')):
        assert said in step and code in CERTS, said
    assert _roles(CERTS, "issue_cert") == '"doctor", "public_health"'
    assert _roles(CHECKUPS, "create_checkup") == '"doctor", "public_health"'
    assert "编码重复会被拒" in step and "同一次体检里项目编码" in CHECKUPS   # P2-1404 的 422


def test_总检限医师_手册引的就是接口回的那句():
    step = _doctor_step()
    refused = re.search(r"这时总检会被拒：\n?\s*「([^」]+)」", step)
    assert refused, step
    assert f'detail="{refused.group(1)}"' in CHECKUPS, refused.group(1)   # P2-1403 的 409
    assert _roles(CHECKUPS, "review_checkup") == '"doctor"'
    public = _chapter("## 第五章 公卫人员（public_health）")
    assert "证明与体检（签发与体检登记）" in public[public.index("### 页面清单"):public.index("### 关键操作")]
    assert "**总检限医师**" in _item(public, "8. **证明与体检**")


def test_管理层一章写死因报告卡与导出_只有管理层能取():
    pages = _chapter("## 第二章 管理层（director）")
    row = next(line for line in pages.splitlines() if line.startswith("| 证明与体检 |"))
    assert "死因报告卡" in row and "批量导出" in row and "签发证明、登记体检、总检不在管理层职责里" in row, row
    assert _roles(CERTS, "death_report_card") == '"director"'
    assert _roles(CERTS, "export_death_report_cards_csv") == '"director"'
