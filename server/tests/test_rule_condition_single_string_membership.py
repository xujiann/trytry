"""统一规则条件写 `变量 in ("单个值")`（漏了逗号）成了子串判断：录入时拒收（P2-1735，第五十一批扫描 AO3-4）。

`("METFORMIN")` 的括号不构成元组，就是字符串 "METFORMIN"，`in` 于是成了子串判断。模块 docstring 把成员运算定义为字符串
枚举判断，扫描实测（`r2_rules.py` C 段）两条规则都录入 201：
- `drug_code in ("METFORMIN")`：drug_code 为 "MET"、为空串都命中；
- 拦截级 `diagnosis_name in ("妊娠期高血压") and …`：诊断「高血压」、空诊断都命中，回执 blocked=true。
试算查不出来：子串判断不报错，样例值下结果也对得上。

修法：录入校验（`validate_condition`）在 `in` / `not in` 左边是变量、右边是单个字符串常量时 422，提示「单个取值写成
("A",) 或用 ==」；「字面量 in 变量」（文本里含不含某段字）照收。求值语义不动。
"""
import pytest

from app.routers.rules import DOMAIN_VARIABLES
from app.rules import RuleError, validate_condition

RX = DOMAIN_VARIABLES["prescription"]


@pytest.mark.parametrize("condition, hint", [
    ('drug_code in ("METFORMIN")', '单个取值写成 ("METFORMIN",) 或用 =='),
    ('diagnosis_name in ("妊娠期高血压") and drug_code in ("A02", "C09AA01")', '单个取值写成 ("妊娠期高血压",) 或用 =='),
    ('drug_code not in ("ASPIRIN")', '单个取值写成 ("ASPIRIN",) 或用 !='),
    ('drug_code in "METFORMIN"', '单个取值写成 ("METFORMIN",) 或用 =='),
    ('not (age > 1 and drug_code in ("WARFARIN"))', '单个取值写成 ("WARFARIN",) 或用 =='),
    ('"A" < drug_code in ("METFORMIN")', '单个取值写成 ("METFORMIN",) 或用 =='),   # 链式比较里的那一环
])
def test_变量in单个字符串_录入时拒收(condition, hint):
    with pytest.raises(RuleError, match="会按子串判断") as caught:
        validate_condition(condition, RX)
    assert hint in str(caught.value)


@pytest.mark.parametrize("condition", [
    'drug_code in ("METFORMIN",)',
    'drug_code in ("A02", "C09AA01")',
    'drug_code not in ("ASPIRIN",)',
    'drug_code == "METFORMIN"',
    '"高血压" in diagnosis_name',          # 字面量 in 变量：包含判断，照收
    '"糖尿病" not in diagnosis_name and age >= 65',
])
def test_元组与包含判断照收(condition):
    validate_condition(condition, RX)


@pytest.mark.parametrize("key, condition, severity", [
    ("p21735-metformin", 'drug_code in ("METFORMIN")', "warning"),
    ("p21735-pih", 'diagnosis_name in ("妊娠期高血压") and drug_code in ("A02", "C09AA01")', "error"),
])
def test_录入端点422_不落库(client, admin, key, condition, severity):
    resp = client.post("/api/rules", headers=admin, json={
        "key": key, "name": key, "domain": "prescription", "condition": condition, "severity": severity})
    assert resp.status_code == 422, resp.text   # 修前 201
    assert "或用 ==" in resp.json()["detail"]
    assert key not in {r["key"] for r in client.get("/api/rules", headers=admin).json()}


def test_单值元组写法照收_试算不再被子串命中(client, admin):
    resp = client.post("/api/rules", headers=admin, json={
        "key": "p21735-tuple", "name": "只认二甲双胍", "domain": "prescription", "condition": 'drug_code in ("METFORMIN",)'})
    assert resp.status_code == 201, resp.text
    for drug_code, hit in (("MET", False), ("", False), ("METFORMIN", True)):
        result = client.post("/api/rules/evaluate", headers=admin, json={
            "domain": "prescription", "variables": {"drug_code": drug_code}}).json()
        assert ("p21735-tuple" in {h["key"] for h in result["hits"]}) is hit, (drug_code, result)
