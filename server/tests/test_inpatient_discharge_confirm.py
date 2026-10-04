"""住院页「出院」先确认、写明后果并标「不可撤销」（P2-1334，第三十九批扫描 AC4-2）。

修前：在院那一行的红色「出院」按钮（紧挨「病案首页」）点一下就 POST `/discharge`——首页已填、费用已结的在院病人误点一次
就办完出院：全部执行中医嘱被停、床位释放（随即可被别人占用）、派出院随访并通知居民「您已办理出院」、出院事件发给慢专病
派生随访计划，而出院撤不回（P2-753）。P2-43 立的规矩是「点一下就生效的不可逆操作必须先确认」，确认闸门
（`test_frontend_destructive_confirm_guard.py`）的判据却不认 `/discharge`：判为破坏性调用 30 条，含出院的 0 条，一直绿着。

闸门管「确认在不在」；这里管确认把后果说没说清。点了取消仍在院、确定才出院，由端到端
`test_住院页出院先确认_取消仍在院_确定才出院` 按接口核对。
"""
import re
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")


def _before_discharge_post() -> str:
    """住院页点击处理里「出院」分支开头到 POST 地址之间的那一段。"""
    start = SOURCE.index("async function renderInpatient()")
    body = SOURCE[start:SOURCE.index("\nasync function ", start + 1)]
    branch = body[body.index("if (d.discharge)"):]
    return branch[:branch.index("/discharge`")]


def test_出院先弹确认_取消即不办():
    before_post = _before_discharge_post()
    assert re.search(r"if \(!await spdModal\(", before_post), before_post   # 修前直接 POST，没有确认


def test_确认写明出院的后果并标不可撤销():
    before_post = _before_discharge_post()
    for consequence in ("不可撤销", "医嘱", "床位", "出院随访", "通知", "慢专病"):
        assert consequence in before_post, (consequence, before_post)
