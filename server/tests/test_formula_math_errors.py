"""公式 / 规则求值器把 Python 的运算异常原样漏出去：录入校验、出报表、规则试算直接 500（P2-339）。

`app/formula.py` 的调用方（绩效公式录入与期末报告、基金分配、统一规则引擎）只兜 `FormulaError` / `RuleError`，
而求值器在这几处抛的是别的：

- 参数个数不对：`max(x)`、`abs(x, y)` 是 TypeError；`round(x, 1, 99)` 则被悄悄截成两个参数；
- 0 的负数次方：`x ** -1` 遇 0 是 ZeroDivisionError——模块自己的规矩是「除零返回 0」；
- 负数开非整数次方：`(x - 10) ** 0.5` 得复数，`float()` 时 TypeError；
- 溢出：`((x ** 8) ** 8) ** 8` 是 OverflowError；`x * 1e308 * 10` 不抛异常、得 inf，序列化成 JSON 时 500；
- 规则求值的兜底只收 TypeError / ValueError，漏了 ArithmeticError。

修法：参数个数按表查；0 的负数次方与除零同口径返回 0；其余算不出有限实数的一律 `FormulaError`。
录入校验用哑值 1 代入，哑值下算不出的不算写错（换组真实取值就算得出），但后面的部分照样查语法、变量与参数个数。
"""
from datetime import date

import pytest

from app.formula import FormulaError, evaluate, validate
from app.rules import RuleError, evaluate_condition


# ================================================================ 求值器
@pytest.mark.parametrize(("expression", "message"), [
    ("max(a)", "max 至少需要 2 个参数"),
    ("min(a)", "min 至少需要 2 个参数"),
    ("abs(a, b)", "abs 最多接受 1 个参数"),
    ("round(a, 1, 99)", "round 最多接受 2 个参数"),   # 修前悄悄截成 round(a, 1)
])
def test_函数参数个数不对_求值与录入都报公式错(expression, message):
    with pytest.raises(FormulaError, match=message):
        evaluate(expression, {"a": 1.23, "b": 2})
    with pytest.raises(FormulaError, match=message):
        validate(expression, {"a", "b"})


def test_零的负数次方与除零同口径返回0():
    assert evaluate("a ** -1", {"a": 0}) == 0
    assert evaluate("(a - 1) ** -0.5", {"a": 1}) == 0
    assert evaluate("a ** -1", {"a": 4}) == 0.25


@pytest.mark.parametrize(("expression", "variables", "message"), [
    ("(a - 10) ** 0.5", {"a": 1}, "负数不能开非整数次方"),
    ("((a ** 8) ** 8) ** 8", {"a": 1e10}, "超出数值范围"),
    ("a * 1e308 * 10", {"a": 1}, "超出数值范围"),              # 不抛异常、得 inf
    ("round(a * 1e308 * 10)", {"a": 1}, "超出数值范围"),
    ("round(a, 1e400)", {"a": 1}, "小数位数不是有效数值"),
    ("a + 1", {"a": 10 ** 400}, "变量 a 的值超出数值范围"),
])
def test_算不出有限实数_报公式错而不是漏出运算异常(expression, variables, message):
    with pytest.raises(FormulaError, match=message):
        evaluate(expression, variables)


def test_录入校验_哑值下算不出不算写错_后面照样查():
    validate("(a - 2 * b) ** 0.5", {"a", "b"})                # 哑值 a=b=1 开负数的平方根，真实取值算得出
    with pytest.raises(FormulaError, match="未知变量：nope"):
        validate("(a - 2 * b) ** 0.5 + nope", {"a", "b"})
    with pytest.raises(FormulaError, match="max 至少需要 2 个参数"):
        validate("(a - 2 * b) ** 0.5 + max(a)", {"a", "b"})


def test_规则条件_运算异常收成规则错():
    assert evaluate_condition("daily_dose ** -1 > 0.5", {"daily_dose": 0}) is False
    with pytest.raises(RuleError, match="负数不能开非整数次方"):
        evaluate_condition("(age - 60) ** 0.5 > 1", {"age": 40})
    with pytest.raises(RuleError, match="超出数值范围"):
        evaluate_condition("x > 1", {"x": 10 ** 400})      # 客户端传几百位的整数
    with pytest.raises(RuleError, match="超出数值范围"):
        evaluate_condition("x * 2 > 1", {"x": 10 ** 400})


# ================================================================ 端点
def test_录入绩效公式_参数个数不对是422(client, admin):
    got = client.post("/api/analytics/formulas", headers=admin,
                      json={"key": "p2339_max", "name": "P2339 单参 max", "expression": "max(encounters)"})
    assert got.status_code == 422, got.text   # 修前 TypeError（500）
    assert "max 至少需要 2 个参数" in got.json()["detail"]


def test_期末绩效报告_某机构算不出只记该项错_报表照出(client, admin):
    org = client.post("/api/organizations", headers=admin,
                      json={"name": "P2339 无业务量卫生院", "org_type": "township", "level": "township"}).json()["id"]
    for key, expression in (("p2339_sqrt", "(encounters - 1) ** 0.5"), ("p2339_inv", "encounters ** -1")):
        created = client.post("/api/analytics/formulas", headers=admin,
                              json={"key": key, "name": key, "expression": expression, "weight": 10})
        assert created.status_code == 201, created.text
    got = client.get("/api/analytics/performance-report", headers=admin,
                     params={"period": date.today().strftime("%Y-%m")})
    assert got.status_code == 200, got.text   # 修前：本期 0 诊疗量，(0 - 1) ** 0.5 得复数 → 500
    items = {i["key"]: i for i in next(o for o in got.json()["orgs"] if o["org_id"] == org)["items"]}
    assert items["p2339_sqrt"]["value"] is None
    assert "负数不能开非整数次方" in items["p2339_sqrt"]["error"]
    assert items["p2339_inv"]["value"] == 0          # 修前 ZeroDivisionError（500）


def test_统一规则试算_零剂量不再500(client, admin):
    created = client.post("/api/rules", headers=admin, json={
        "key": "p2339_inv_dose", "name": "P2339 剂量倒数", "domain": "prescription",
        "condition": "daily_dose ** -1 > 0.5", "severity": "warning"})
    assert created.status_code == 201, created.text
    got = client.post("/api/rules/evaluate", headers=admin,
                      json={"domain": "prescription", "variables": {"daily_dose": 0}})
    assert got.status_code == 200, got.text   # 修前 ZeroDivisionError 漏出 evaluate_condition 的兜底（500）
    body = got.json()
    assert body["errors"] == [] and body["hits"] == []
