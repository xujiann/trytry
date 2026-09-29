"""统一规则引擎的比较按公式同一精度取整：恰好等于门槛的算命中（P2-892，第二十四批「阈值与边界值」扫描 Z4-5）。

`_eval_condition` 原先拿原始浮点直接比：29/100×100 = 28.999999999999996，条件 `referrals_up / encounters * 100 >= 29`
在上转率恰好 29% 时判不中；57、58 同样不中，7、14 却中——恰好等于门槛的机构命中与否取决于具体数字。公式求值器
（`formula.evaluate`）算同一个式子先取 4 位小数得 29.0，慢专病考核就是拿它和目标比。修后比较两侧按同一精度取整。
"""
import pytest

from app.rules import evaluate_condition

CONDITION = "referrals_up / encounters * 100 >= RATE"


@pytest.mark.parametrize("rate", [7, 14, 29, 57, 58])
def test_恰好等于门槛_判命中(rate):
    assert evaluate_condition(CONDITION.replace("RATE", str(rate)),
                              {"referrals_up": rate, "encounters": 100}) is True   # 修前 29、57、58 为 False


def test_差一点照旧不中_等值与成员判断同一精度():
    assert evaluate_condition(CONDITION.replace("RATE", "29"), {"referrals_up": 28.99, "encounters": 100}) is False
    assert evaluate_condition("x * 100 == 29", {"x": 0.29}) is True          # 0.29×100 = 28.999999999999996
    assert evaluate_condition("x * 100 in (29, 30)", {"x": 0.29}) is True


def test_经接口按域求值_恰好29命中(client, admin):
    made = client.post("/api/rules", headers=admin, json={
        "key": "P2892_UP29", "name": "上转率达 29%", "domain": "org_performance",
        "condition": "referrals_up / encounters * 100 >= 29", "severity": "info"})
    assert made.status_code == 201, made.text
    got = client.post("/api/rules/evaluate", headers=admin, json={
        "domain": "org_performance", "variables": {"referrals_up": 29, "encounters": 100}})
    assert got.status_code == 200, got.text
    assert "P2892_UP29" in [hit["key"] for hit in got.json()["hits"]]   # 修前不在
