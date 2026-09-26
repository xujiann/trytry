"""居民端自查把多选题画成单选下拉：多选题最多答一项，风险被低估、「申请专病服务」的提示出不来（P2-363）。

量表的题型有单选 / 多选 / 数值（管理端量表构建器给「多选」），`score_scale` 对多选题按数组逐项累加；医护端答题与
线上自助随访早就是复选框。居民端自查（`drawItems`）却每题都画成 `<select>`、交卷一题一个字符串：4 项各 1 分的症状题
最多得 1 分。修法：多选题画成复选框、按数组交；只取量表区的控件。另钉后端按数组累加的那一半。
"""
from pathlib import Path

from app.spd.rules import score_scale

M_JS = (Path(__file__).resolve().parents[1] / "app" / "static" / "m" / "m.js").read_text(encoding="utf-8")


def _selfscreen() -> str:
    start = M_JS.index("const drawItems = () => {")
    return M_JS[start:M_JS.index("#spd-screen-msg", start)]


def test_多选题画成复选框_按数组交():
    body = _selfscreen()
    assert 'item.type === "multi"' in body and 'type="checkbox" data-q=' in body   # 修前一律 <select>
    assert "el.checked" in body and ".push(el.value)" in body


def test_只取量表区的控件():
    assert '$("#spd-scale-items").querySelectorAll("[data-q]")' in _selfscreen()


def test_后端按数组逐项累加():
    items = [{"key": "sym", "type": "multi", "options": [{"label": s, "score": 1} for s in ("头晕", "胸闷", "气促", "乏力")]}]
    scoring = {"ranges": [{"min": 0, "max": 1, "risk": "low"}, {"min": 2, "max": None, "risk": "high"}]}
    assert score_scale(items, {"sym": ["头晕", "胸闷", "气促"]}, scoring)["score"] == 3.0
