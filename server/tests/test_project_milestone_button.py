"""已完成 / 已中止的项目不给「加里程碑」（P2-597，第十二批「按钮 vs 状态机」扫描 Z2-7）。

加里程碑的接口早就对这两态 409「项目已完成或已中止，不能再加里程碑」（P1-104，`tests/test_closed_parent_writes.py`
盯着），项目清单却每一行都画「加里程碑」，填完名称、到期日点确定才报错。
"""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")


def test_项目清单只给在办的项目画加里程碑():
    start = PAGE.index('${panel("项目清单"')
    body = PAGE[start:PAGE.index('${panel("里程碑（全部项目）"', start)]
    assert ('(p.status === "done" || p.status === "suspended" ? "" : '
            '`<button class="btn sm" data-ms="${p.id}">加里程碑</button>`)') in body   # 修前无条件画
    assert body.count('data-ms="${p.id}"') == 1
