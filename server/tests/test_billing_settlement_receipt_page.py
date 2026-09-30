"""费用结算页把住院结算回执里的押金冲抵、应补缴、押金余额说出来（P2-914，第二十五批「费用与业务状态」扫描 J3-4 前一半）。

结算回执（`SettlementCreateOut`）住院结算带押金冲抵、应补缴、押金余额三个条件键；页面的结算表单原先走 `postAction`，
成功即整页重画、回执整个丢掉——整个前端没有一处读这三个字段。押金 3000、费用 1000：回执里余额 2000，页面上看不到，
收费员只能再去查押金流水。修后结算成功重画之后把回执写进消息行；门诊回执没有这三个键，照旧只说总额 / 医保 / 自付。
出院时押金余额怎么处理另见待裁定。
"""
from pathlib import Path

SOURCE = (Path(__file__).resolve().parent.parent / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")


def _handler():
    start = SOURCE.index('$("#settle-form").onsubmit')
    return SOURCE[start:SOURCE.index('$("#pay-form").onsubmit', start)]


def test_结算回执的押金三字段写进消息行():
    handler = _handler()
    assert "postAction(" not in handler   # 修前 postAction：成功即重画、回执丢掉
    for key in ("deposit_offset", "payable_after_offset", "deposit_balance"):
        assert f"r.{key}" in handler, key
    assert handler.index("await route()") < handler.index('setMsg("#bill-msg"')   # 先重画再写消息，别被重画冲掉


def test_门诊回执没有押金键时不硬印():
    assert 'r.deposit_offset === undefined ? ""' in _handler()
