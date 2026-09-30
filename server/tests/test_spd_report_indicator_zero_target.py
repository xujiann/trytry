"""慢专病报告的指标段把目标值 0 当成「没设目标」（P2-995，第二十八批「空值、空串、0 与『未填』」扫描 F1-6）。

`reporting._indicator` 拼段落文字时写 `if indicator.target_value`：目标为 0 的指标（「目标 0 例投诉」，计分那边写明分档计分的
0 目标合法）不印「（目标 0）」，和没设目标印成一个样；考核指标库页面用 `?? "—"` 区分 0 与未设。

修法：按 `is not None` 判。
"""
from types import SimpleNamespace

import pytest

from app.spd import reporting
from app.spd.routers import assess


def _indicator(target):
    return SimpleNamespace(code="P2996", name="P2996 投诉数", object_type="org", formula="", target_value=target,
                           version="v1")


@pytest.mark.parametrize("target, suffix", [(0.0, "（目标 0.0）"), (None, ""), (5.0, "（目标 5.0）")])
def test_目标0照印_未设不印(monkeypatch, target, suffix):
    indicator = _indicator(target)
    monkeypatch.setattr(assess, "effective_versions", lambda db, codes, period: ({"P2996": indicator}, set()))
    monkeypatch.setattr(assess, "collect_metrics", lambda db, ind, kind, org_id, period: {"total": 0})
    got = reporting._indicator(None, {"key": "indicator", "indicator_code": "P2996", "period": "2026-09"}, 1, "monthly")
    assert got["text"] == "P2996 投诉数：0.0" + suffix, got   # 修前目标 0 不印
