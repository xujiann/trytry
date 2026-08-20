"""特征化测试（characterization / golden-master）——保护 `app/spd/rules.py` 的重构。

    LEGACY CODE  →  TEST PROTECTION  →  REFACTOR
                    （本文件在这里）

`app/spd/rules.py` 是"6 套并行规则引擎"技术债的一员（见 docs/TECH_DEBT.md），
是明确的重构候选（未来要与 app/rules.py / app/formula.py 收敛）。但它此前几乎没有
直接测试——只有 test_spd_flow.py 用过一次 `evaluate`。在动它之前，先用一层
特征化测试把**当前行为**钉死。

特征化测试与常规单元测试的区别：它断言的是"代码现在实际怎么做"，**不是**
"代码应该怎么做"。因此本文件刻意把几处反直觉的现状行为也一并钉住（下方标了
「⚠ 现状怪癖」）。重构时若这些断言变红，意味着行为变了——要么是重构引入的
回归，要么是有意修正；无论哪种，都必须是一次**明确的、被看见的**决定，而不是
悄悄漂移。所以：**修改被测行为前不要改这里的断言来迁就**，先确认行为变化是否
是本意。
"""
from __future__ import annotations

import pytest

from app.spd.rules import (
    RuleError,
    evaluate,
    grade_abnormal,
    judge_level,
    score_scale,
    screen,
    validate_conditions,
)


# ---------------------------------------------------------------------------
# evaluate / _match_one —— 单条件与多条件求值
# ---------------------------------------------------------------------------

class TestEvaluateBasics:
    def test_空条件不命中任何人(self):
        # 关键口径：空规则返回 (False, [])，不是"全命中"。
        assert evaluate([], {"age": 50}) == (False, [])

    def test_mode_all_需全部满足(self):
        conds = [
            {"field": "age", "op": ">=", "value": 60},
            {"field": "bp_sys", "op": ">=", "value": 140},
        ]
        hit, matched = evaluate(conds, {"age": 65, "bp_sys": 150}, mode="all")
        assert hit is True
        assert len(matched) == 2

        hit2, matched2 = evaluate(conds, {"age": 65, "bp_sys": 120}, mode="all")
        assert hit2 is False
        assert len(matched2) == 1  # 命中明细仍返回已命中的那条

    def test_mode_any_任一满足即命中(self):
        conds = [
            {"field": "bp_sys", "op": ">=", "value": 180},
            {"field": "glucose_fasting", "op": ">=", "value": 16},
        ]
        hit, matched = evaluate(conds, {"bp_sys": 190, "glucose_fasting": 7}, mode="any")
        assert hit is True
        assert len(matched) == 1

    def test_缺字段判不命中(self):
        # 配了本地没采集的指标不该报错，只是不命中。
        hit, matched = evaluate([{"field": "ldl", "op": ">", "value": 3.4}], {"age": 50})
        assert hit is False
        assert matched == []


class TestMatchOperators:
    def test_数值比较符(self):
        f = {"v": 10}
        assert evaluate([{"field": "v", "op": ">", "value": 5}], f)[0] is True
        assert evaluate([{"field": "v", "op": ">=", "value": 10}], f)[0] is True
        assert evaluate([{"field": "v", "op": "<", "value": 10}], f)[0] is False
        assert evaluate([{"field": "v", "op": "<=", "value": 10}], f)[0] is True

    def test_between_闭区间(self):
        assert evaluate([{"field": "bmi", "op": "between", "value": [24, 28]}], {"bmi": 24})[0] is True
        assert evaluate([{"field": "bmi", "op": "between", "value": [24, 28]}], {"bmi": 28})[0] is True
        assert evaluate([{"field": "bmi", "op": "between", "value": [24, 28]}], {"bmi": 28.1})[0] is False

    def test_非数值遇数值比较符判不命中(self):
        assert evaluate([{"field": "v", "op": ">", "value": 5}], {"v": "高"})[0] is False

    def test_in_与_not_in_标量(self):
        assert evaluate([{"field": "gender", "op": "in", "value": ["男", "女"]}], {"gender": "男"})[0] is True
        assert evaluate([{"field": "gender", "op": "not_in", "value": ["男"]}], {"gender": "女"})[0] is True

    def test_in_左值为列表_诊断编码(self):
        # 诊断是列表；任一诊断落在池子里即命中。
        cond = {"field": "diagnosis", "op": "in", "value": ["I10", "E11"]}
        assert evaluate([cond], {"diagnosis": ["J45", "I10"]})[0] is True
        assert evaluate([cond], {"diagnosis": ["J45"]})[0] is False

    def test_contains_子串匹配(self):
        cond = {"field": "surgery", "op": "contains", "value": "冠脉"}
        assert evaluate([cond], {"surgery": ["冠脉搭桥术"]})[0] is True
        assert evaluate([cond], {"surgery": ["阑尾切除术"]})[0] is False

    def test_相等比较用字符串语义_现状怪癖(self):
        # ⚠ 现状怪癖：== / != 走 str(actual) == str(expected)，不是类型化相等。
        # 于是整数 50 与整数 50 相等（意料之中），"50"（字符串）与 50 也相等（意料之外）。
        # 重构成类型化相等会改变此行为——因此在这里钉死现状。
        assert evaluate([{"field": "age", "op": "==", "value": 50}], {"age": 50})[0] is True
        assert evaluate([{"field": "age", "op": "==", "value": 50}], {"age": "50"})[0] is True
        assert evaluate([{"field": "age", "op": "!=", "value": 50}], {"age": 51})[0] is True

    def test_exists_存在与不存在(self):
        assert evaluate([{"field": "ua", "op": "exists", "value": True}], {"ua": 480})[0] is True
        # 空值视为不存在
        assert evaluate([{"field": "ua", "op": "exists", "value": True}], {"ua": None})[0] is False
        assert evaluate([{"field": "ua", "op": "exists", "value": True}], {"ua": ""})[0] is False

    def test_exists_false_对缺失字段返回真_现状怪癖(self):
        # ⚠ 现状怪癖：field 根本不在 facts 里时，`exists:false` 判定为命中（"确实不存在"）。
        # 这与"缺字段一律不命中"的总原则相反，是 exists 的专门例外（rules.py:121）。
        assert evaluate([{"field": "ua", "op": "exists", "value": False}], {})[0] is True
        # 而 exists:true 对缺失字段则不命中
        assert evaluate([{"field": "ua", "op": "exists", "value": True}], {})[0] is False


# ---------------------------------------------------------------------------
# screen —— 纳入/排除人群判定
# ---------------------------------------------------------------------------

class TestScreen:
    include = [{"field": "diagnosis", "op": "in", "value": ["I10"]}]
    exclude = [{"field": "age", "op": "<", "value": 18}]

    def test_命中纳入且未命中排除_suspect(self):
        out = screen(self.include, self.exclude, {"diagnosis": ["I10"], "age": 60})
        assert out["result"] == "suspect"
        assert len(out["matched"]) == 1
        assert out["excluded_by"] == []

    def test_排除优先于纳入(self):
        # 同时符合纳入与排除时，排除压过纳入。
        out = screen(self.include, self.exclude, {"diagnosis": ["I10"], "age": 10})
        assert out["result"] == "excluded"
        assert out["matched"] == []
        assert len(out["excluded_by"]) == 1

    def test_未命中纳入_normal(self):
        out = screen(self.include, self.exclude, {"diagnosis": ["J45"], "age": 60})
        assert out["result"] == "normal"

    def test_空规则_normal(self):
        assert screen([], [], {"age": 60})["result"] == "normal"


# ---------------------------------------------------------------------------
# judge_level —— 单指标等级
# ---------------------------------------------------------------------------

class TestJudgeLevel:
    def test_高于上限_high(self):
        assert judge_level(150, 90, 140) == "high"

    def test_低于下限_low(self):
        assert judge_level(80, 90, 140) == "low"

    def test_区间内_normal(self):
        assert judge_level(120, 90, 140) == "normal"

    def test_边界值取_normal_现状怪癖(self):
        # ⚠ 现状怪癖：> / < 是严格不等，等于上限或下限均判 normal。
        assert judge_level(140, 90, 140) == "normal"
        assert judge_level(90, 90, 140) == "normal"

    def test_无目标或无值_normal(self):
        assert judge_level(200, None, None) == "normal"
        assert judge_level(None, 90, 140) == "normal"
        assert judge_level("非数值", 90, 140) == "normal"


# ---------------------------------------------------------------------------
# score_scale —— 量表评分
# ---------------------------------------------------------------------------

class TestScoreScale:
    items = [
        {"key": "q1", "type": "single", "options": [
            {"label": "从不", "score": 0}, {"label": "经常", "score": 3}]},
        {"key": "q2", "type": "multi", "options": [
            {"label": "头晕", "score": 1}, {"label": "胸闷", "score": 2}]},
        {"key": "q3", "type": "number", "score_per_unit": 0.5},
    ]
    scoring = {"ranges": [
        {"min": 0, "max": 2, "risk": "低危", "advice": "常规随访"},
        {"min": 3, "max": None, "risk": "高危", "advice": "尽快复诊"},
    ]}

    def test_单选多选数值累加(self):
        out = score_scale(self.items, {"q1": "经常", "q2": ["头晕", "胸闷"], "q3": 4}, self.scoring)
        # 3 (单选) + 1+2 (多选) + 0.5*4=2 (数值) = 8.0
        assert out["score"] == 8.0
        assert out["risk_level"] == "高危"
        assert out["advice"] == "尽快复诊"
        assert out["answered"] == 3
        assert out["total_items"] == 3

    def test_未作答的题不计入_answered(self):
        # ⚠ 现状：未作答（key 不在 answers）直接跳过，既不加分也不计入 answered。
        out = score_scale(self.items, {"q1": "从不"}, self.scoring)
        assert out["score"] == 0.0
        assert out["risk_level"] == "低危"
        assert out["answered"] == 1
        assert out["total_items"] == 3

    def test_命中第一个匹配区间即停(self):
        out = score_scale(self.items, {"q1": "经常"}, self.scoring)  # 3 分
        assert out["risk_level"] == "高危"  # 落在 [3, None]


# ---------------------------------------------------------------------------
# grade_abnormal —— 问卷异常分级
# ---------------------------------------------------------------------------

class TestGradeAbnormal:
    def test_取命中的最高级别而非最后一条(self):
        # 书写顺序把 high 放前、low 放后；结果应是 high（不受顺序影响）。
        rules = [
            {"when": {"field": "bp_sys", "op": ">=", "value": 180}, "level": "high", "action": "立即转诊"},
            {"when": {"field": "bp_sys", "op": ">=", "value": 140}, "level": "low", "action": "复测"},
        ]
        assert grade_abnormal(rules, {"bp_sys": 190}) == ("high", "立即转诊")
        # 顺序颠倒，结果不变
        assert grade_abnormal(list(reversed(rules)), {"bp_sys": 190}) == ("high", "立即转诊")

    def test_无命中返回_none(self):
        rules = [{"when": {"field": "bp_sys", "op": ">=", "value": 180}, "level": "high", "action": "x"}]
        assert grade_abnormal(rules, {"bp_sys": 120}) == ("none", "")

    def test_规则缺_level_默认_low_现状怪癖(self):
        # ⚠ 现状：命中但未写 level 的规则，级别默认为 "low"（rules.py:274）。
        rules = [{"when": {"field": "spo2", "op": "<", "value": 90}, "action": "吸氧"}]
        assert grade_abnormal(rules, {"spo2": 85}) == ("low", "吸氧")

    def test_空_when_跳过(self):
        rules = [{"when": {}, "level": "high", "action": "x"}]
        assert grade_abnormal(rules, {"spo2": 85}) == ("none", "")

    def test_空规则返回_none(self):
        assert grade_abnormal([], {"bp_sys": 190}) == ("none", "")


# ---------------------------------------------------------------------------
# validate_conditions —— 配置期结构校验
# ---------------------------------------------------------------------------

class TestValidateConditions:
    def test_正常条件规范化(self):
        out = validate_conditions([{"field": "age", "op": ">=", "value": 60, "label": "老年"}])
        assert out == [{"field": "age", "op": ">=", "value": 60, "label": "老年"}]

    def test_空或None返回空列表不报错(self):
        assert validate_conditions([]) == []
        assert validate_conditions(None) == []

    def test_label_截断到64字符(self):
        out = validate_conditions([{"field": "age", "op": ">", "value": 1, "label": "长" * 100}])
        assert len(out[0]["label"]) == 64

    def test_非对象条件报错(self):
        with pytest.raises(RuleError):
            validate_conditions(["不是对象"])

    def test_缺field报错(self):
        with pytest.raises(RuleError):
            validate_conditions([{"op": ">", "value": 1}])

    def test_未知比较符报错(self):
        with pytest.raises(RuleError):
            validate_conditions([{"field": "age", "op": "≈", "value": 1}])

    def test_between_value_必须两元素(self):
        with pytest.raises(RuleError):
            validate_conditions([{"field": "bmi", "op": "between", "value": [24]}])
