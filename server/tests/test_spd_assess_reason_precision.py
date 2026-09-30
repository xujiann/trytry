"""慢专病考核按 4 位原值判「未达目标」，扣分理由却印 `round(实际, 2)`：写「未达目标值90.0（实际90.0）」（P2-991，第二十八批
「取整与精度发生在哪一步」扫描 F2-7）。

指标值来自公式求值（取 4 位小数，P2-892），`score_of` 拿它和目标比；理由里的「实际」却取两位。分母上万（县级在管人数量级）
时 18000 / 20001 = 89.9955：判未达 90、按比例得 99.99 或 100.0，理由写「未达目标值90.0（实际90.0）」，看的人以为达标了还被扣分。

修法：理由里的「实际」按判定用的 4 位印；整数、一两位小数的实际值印法不变。
"""
from types import SimpleNamespace

from app.spd.routers.assess import score_of


def _indicator(target):
    return SimpleNamespace(score_rule={"type": "ratio", "full": 100}, target_value=target)


def test_理由里的实际按判定精度印():
    reason = score_of(_indicator(90.0), 89.9955)[1]
    assert reason == "未达目标值90.0（实际89.9955）", reason   # 修前（实际90.0）


def test_整数与两位小数的实际值印法不变():
    assert score_of(_indicator(100.0), 50.0)[1] == "未达目标值100.0（实际50.0）"
    assert score_of(_indicator(90.0), 85.25)[1] == "未达目标值90.0（实际85.25）"
    assert score_of(_indicator(90.0), 90.0) == (100.0, "")
