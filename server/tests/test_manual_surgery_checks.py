"""用户手册的手术几条跟上第四十一批加的校验（P2-1396～P2-1399、P2-1402 之后的手册同步）。

那几条修复只改了接口与页面：同一患者同一天时段重叠的另一台排班 409（P2-1397）、术中记录转归「死亡」之后同住院不再收申请 /
批准 / 排班（P2-1396）、已登记高值耗材的申请不能否决（P2-1399）、本次住院此前没有手术时勾不上「非计划重返手术室」（P2-1398）、
手术间名前带所属医院（P2-1402）。手册原先只写「同手术间时段重叠会被拦下」「系统不做推断」，照手册做会碰上手册里没写的
409 / 422；排班的人是经办与管理层，经办一章一句没写手术排班。§13 把「有手册条目」算进模块功能完整。

这里钉住手册引的报错原文就是接口回的那句、写给经办的排班角色就是接口收的角色——接口的话改了，这里提醒把手册一起改。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANUAL = (ROOT / "docs" / "用户手册.md").read_text(encoding="utf-8")
SURGERY = (ROOT / "server" / "app" / "routers" / "surgery.py").read_text(encoding="utf-8")


def _chapter(title: str) -> str:
    start = MANUAL.index(title)
    return MANUAL[start:MANUAL.index("\n## ", start + 1)]


def _item(chapter: str, head: str) -> str:
    start = chapter.index(head)
    nxt = re.search(r"\n\d+\. \*\*|\n---", chapter[start + len(head):])
    return chapter[start:start + len(head) + (nxt.start() if nxt else len(chapter))]


def test_非计划重返勾不上_手册引的就是接口回的那句():
    quoted = re.search(r"会提示「([^」]+)」", _item(_chapter("## 第三章 医师（doctor）"), "14. **非计划重返手术室**"))
    assert quoted, "手册第三章第 14 条没写勾不上时的提示（P2-1398 之后照手册勾会碰上 422）"
    assert f'detail="{quoted.group(1)}"' in SURGERY, quoted.group(1)


def test_医师一章的手术写明同一患者重叠与术中死亡之后():
    step = _item(_chapter("## 第三章 医师（doctor）"), "10. **手术**")
    assert "同一患者同一天时段重叠的另一台未取消排班也会被拦下" in step, step
    assert "同一患者的手术时段不能重叠" in SURGERY                         # P2-1397 的 409
    assert "转归选了「死亡」之后，同一次住院不再收新的手术申请" in step and "驳回照常" in step, step
    assert "不可再申请手术" in SURGERY and "不可审批通过" in SURGERY and "不可排班" in SURGERY   # P2-1396 的三处 409


def test_管理层一章写明已登记耗材的申请不能否决():
    pages = _chapter("## 第二章 管理层（director）")
    row = next(line for line in pages.splitlines() if line.startswith("| 手术麻醉 |"))
    assert "已按申请登记了高值耗材的不能否决" in row, row
    assert "def _refuse_if_implanted(" in SURGERY                           # P2-1399


def test_经办一章补上手术排班_角色与接口一致():
    chapter = _chapter("## 第六章 经办人员（operator）")
    assert "手术麻醉（排班）" in chapter[chapter.index("### 页面清单"):chapter.index("### 关键操作")]
    step = _item(chapter, "11. **手术排班**")
    assert "手术间名前带所属医院" in step and "同一患者同一天时段重叠" in step, step
    assert 'dependencies=[Depends(require_roles("operator", "director"))],\n)\ndef schedule_surgery(' in SURGERY
