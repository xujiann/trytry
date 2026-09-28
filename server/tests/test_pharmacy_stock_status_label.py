"""药房库存表的状态不再把没人判过的写成「正常」（P2-706，第十七批「缺失 vs 零」扫描 U1-8）。

缺药按阈值判，阈值 0 是没配预警、永远不触发（P1-146 定的口径）。库存表原先「不在缺药名单里 → 绿色正常」：库存 0、阈值 0
的也是「正常」；批次入库、调入、采购验收新建的库存阈值都是 0，整张表几乎全是没人判过的「正常」。修后：缺药（按阈值）/
无库存（数量 ≤ 0）/ 未设预警（阈值 0）/ 正常。缺药的判据不变。
"""
from pathlib import Path

from jssrc import strip_comments

CORE = strip_comments((Path(__file__).resolve().parents[1] / "app" / "static" / "core.js").read_text(encoding="utf-8"))


def test_库存状态分四种_不把没判过的写成正常():
    assert "const stockTag = (s) => alertIds.has(s.id) ? '<span class=\"tag red\">缺药</span>'" in CORE
    assert "s.quantity <= 0 ? '<span class=\"tag orange\">无库存</span>'" in CORE
    assert "s.threshold ? '<span class=\"tag green\">正常</span>' : '<span class=\"tag\">未设预警</span>'" in CORE
    assert "<td>${stockTag(s)}</td>" in CORE
    # 修前：不在缺药名单里一律绿色「正常」
    assert "alertIds.has(s.id) ? '<span class=\"tag red\">缺药</span>' : '<span class=\"tag green\">正常</span>'" not in CORE
