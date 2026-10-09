"""统一规则录入的试算被链式比较短路：后半截写错照样录得进去，上线后每次求值都报错（P2-1736，第五十一批扫描 AO3-5）。

`validate_condition` 用样例值试算一次（P2-352 又把变量名逐个对了一遍），可链式比较一环为假就不再往下算：下面四条在样例
age=40 下第一环 `65 <= 40` 就是假，后半截从没被算到，录入都 201；扫描实测（`r2_rules.py` B 段）age=70 试算时四条全进
errors——「类型不可比较：70.0 与 '80'」「只允许调用 min / max / round / abs」「len() 只能作用于文本变量」「max 至少需要 2 个
参数」，规则形同虚设。P2-352 只补了变量名这一半，`validate_condition` 的 docstring 讲的正是这个缺口。

修法：录入校验对每个比较逐环都算、两两查可比性，不短路；函数名与参数个数按白名单静态核。求值语义不动。样例值下算不出
有限实数的（负数开方）不算写错，与公式录入校验同一个道理。
"""
import pytest

from app.routers.rules import DOMAIN_VARIABLES
from app.rules import RuleError, evaluate_condition, validate_condition

RX = DOMAIN_VARIABLES["prescription"]

#: 扫描报告里的四条（样例 age=40，第一环为假）与录入时该给的原因
SHORT_CIRCUITED = [
    ('65 <= age < "80"', "类型不可比较：40.0 与 '80'"),
    ("65 <= age < maxx(age, 80)", "只允许调用 min / max / round / abs / len"),
    ("65 <= age < len(age)", "len() 只能作用于文本变量"),
    ("65 <= age < max(age)", "max 至少需要 2 个参数"),
]


@pytest.mark.parametrize("condition, reason", SHORT_CIRCUITED + [
    ("65 <= age < abs(age, 1)", "abs 最多接受 1 个参数"),
    ("65 <= age < round(age, ndigits=1)", "函数调用不支持关键字参数"),
    ("65 <= age < len(drug_code, 1)", "len() 只接受一个参数"),
    ('is_pregnant or 65 <= age < "80"', "类型不可比较：40.0 与 '80'"),   # 嵌在布尔运算里的链式比较同样逐环查
    ("not (65 <= age < max())", "函数调用至少需要一个参数"),
])
def test_链式比较短路掉的后半截_录入时就报(condition, reason):
    with pytest.raises(RuleError) as caught:
        validate_condition(condition, RX)
    assert str(caught.value) == reason


@pytest.mark.parametrize("condition", [
    "65 <= age < 80",
    "0 < age < 18 and daily_dose > 100",
    "65 <= age < max(age, 80)",
    "0 < len(drug_code) < 20",
    'drug_code in ("A02", "C09AA01") and 0 < days <= 30',
    # 样例值下算不出有限实数（40 - 60 开平方）不算写错：age ≥ 65 时算得出
    "65 <= age < (age - 60) ** 0.5 * 10",
])
def test_写对的照收(condition):
    validate_condition(condition, RX)


def test_求值语义不动_链式比较照旧一环为假即停():
    # 后半截照旧不算：age=40 时第一环为假直接判不中，不因后半截的类型问题报错
    assert evaluate_condition('65 <= age < "80"', {"age": 40.0}) is False
    with pytest.raises(RuleError, match="类型不可比较"):
        evaluate_condition('65 <= age < "80"', {"age": 70.0})


@pytest.mark.parametrize("condition, reason", SHORT_CIRCUITED)
def test_录入端点422_不落库(client, admin, condition, reason):
    key = f"p21736-{SHORT_CIRCUITED.index((condition, reason))}"
    resp = client.post("/api/rules", headers=admin, json={
        "key": key, "name": "高龄", "domain": "prescription", "condition": condition, "severity": "error"})
    assert resp.status_code == 422, resp.text   # 修前 201
    assert resp.json() == {"detail": reason}
    assert key not in {r["key"] for r in client.get("/api/rules", headers=admin).json()}


def test_写对的链式比较照收_试算命中(client, admin):
    resp = client.post("/api/rules", headers=admin, json={
        "key": "p21736-ok", "name": "高龄", "domain": "prescription", "condition": "65 <= age < 80", "severity": "error"})
    assert resp.status_code == 201, resp.text
    result = client.post("/api/rules/evaluate", headers=admin, json={
        "domain": "prescription", "variables": {"age": 70}}).json()
    assert "p21736-ok" in {h["key"] for h in result["hits"]} and result["errors"] == []
