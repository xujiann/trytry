"""统一规则录入的试算漏看链式比较后半截的变量：写错的变量名照收，上线后每次求值都报错（P2-352）。

`validate_condition` 用各域样例值试算一次——链式比较一环为假就不再往下算：`65 <= age < max_agee` 在样例 age=40 时
第一环就是假，写错的 `max_agee` 从没被求值，录入 201；此后每次 age ≥ 65 的真求值都记一条「未知变量」，规则形同虚设。
修法：试算之外把表达式里引用的变量（函数名除外）逐个对一遍。
"""
import pytest

from app.rules import RuleError, validate_condition


@pytest.mark.parametrize("condition", ["65 <= age < max_agee", "0 > daily_dose < nosuchvar"])
def test_链式比较后半截的变量也要认识(condition):
    with pytest.raises(RuleError, match="未知变量"):
        validate_condition(condition, {"age": 40.0, "daily_dose": 500.0})


def test_函数名与认识的变量照收():
    validate_condition("max(daily_dose, age) > 1 and len(drug_code) > 0 and not is_pregnant",
                       {"age": 40.0, "daily_dose": 500.0, "drug_code": "M", "is_pregnant": False})


def test_录入端点422(client, admin):
    resp = client.post("/api/rules", headers=admin, json={
        "key": "p2352_chain", "name": "P2352 高龄", "domain": "prescription", "condition": "65 <= age < max_agee"})
    assert resp.status_code == 422, resp.text   # 修前 201
    assert resp.json() == {"detail": "未知变量：max_agee"}
