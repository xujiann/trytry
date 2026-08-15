"""慢专病规则求值：纳入 / 排除 / 转诊触发 / 问卷异常分级共用一套判定。

四处业务（专病纳入规则、排除规则、转诊触发规则、随访问卷异常规则）在招标文件里
是四段话，但形状完全相同：**一组「字段 + 比较符 + 值」的条件，对一份事实求值**。
所以这里只写一个求值器，四处共用——写四遍的代价不是多打字，是四份各自演化的
比较符语义（"in 对空列表算不算命中"这种问题会有四个答案）。

## 为什么不用表达式字符串

平台已有 `app/formula.py` 的 AST 白名单求值器，用于绩效公式。但那是**算数值**，
这里是**判条件**，且条件要能在管理页面上逐条增删（"再加一条尿酸 > 420"）。
结构化 JSON 能直接渲染成表单行，表达式字符串只能给一个输入框。

## 事实字典（facts）的字段来源

| 字段 | 来源 |
|---|---|
| `age` / `gender` | patients 表 |
| `diagnosis` | 就诊记录诊断编码列表 |
| `surgery` | 手术记录术式名称列表 |
| `orders` | 医嘱关键词列表 |
| `bp_sys` / `bp_dia` / `glucose_fasting` / `ua` / `spo2` / `bmi` … | 最近一次监测值 |
| `risk_level` | 纳管档案风险等级 |
| `followup_overdue_days` | 随访超期天数 |
| `path_overdue` | 路径节点是否逾期 |

缺字段一律判为**不命中**而不是报错：规则是各县自己配的，配了一个本地没采集的
指标不该让整批筛查中断——不命中会体现在筛查结果里，报错只会体现在日志里。
"""
from __future__ import annotations

#: 规则可引用的字段及其中文名，供管理端下拉与文档生成使用。
FIELD_SOURCES: dict[str, str] = {
    "age": "年龄",
    "gender": "性别",
    "diagnosis": "诊断编码",
    "diagnosis_name": "诊断名称",
    "surgery": "手术术式",
    "orders": "医嘱关键词",
    "bp_sys": "收缩压",
    "bp_dia": "舒张压",
    "glucose_fasting": "空腹血糖",
    "glucose_pp2h": "餐后2小时血糖",
    "hba1c": "糖化血红蛋白",
    "ua": "尿酸",
    "spo2": "血氧饱和度",
    "bmi": "体质指数",
    "ldl": "低密度脂蛋白",
    "creatinine": "血肌酐",
    "egfr": "肾小球滤过率",
    "smoking": "吸烟",
    "risk_level": "风险等级",
    "stage": "管理阶段",
    "followup_overdue_days": "随访超期天数",
    "path_overdue": "路径节点逾期",
    "score": "量表得分",
}

#: 比较符及中文名。`in`/`not_in` 的左值可以是标量也可以是列表（诊断是列表）。
OPERATORS: dict[str, str] = {
    "==": "等于",
    "!=": "不等于",
    ">": "大于",
    ">=": "大于等于",
    "<": "小于",
    "<=": "小于等于",
    "in": "属于",
    "not_in": "不属于",
    "contains": "包含",
    "between": "介于",
    "exists": "存在",
}


class RuleError(ValueError):
    """规则结构非法（未知字段 / 未知比较符）。配置期就该拦住，不留到求值期。"""


def validate_conditions(conditions: list[dict]) -> list[dict]:
    """校验一组条件的结构，返回规范化后的条件列表。

    在**写入配置时**调用，而不是求值时——求值发生在批量筛查里，一次几万条，
    那时报错既晚又吵。
    """
    normalized: list[dict] = []
    for raw in conditions or []:
        if not isinstance(raw, dict):
            raise RuleError("条件必须是对象")
        field = str(raw.get("field", "")).strip()
        op = str(raw.get("op", "")).strip()
        if not field:
            raise RuleError("条件缺少 field")
        if op not in OPERATORS:
            raise RuleError(f"未知比较符：{op}")
        if op == "between":
            value = raw.get("value")
            if not isinstance(value, list) or len(value) != 2:
                raise RuleError("between 的 value 必须是 [下限, 上限]")
        normalized.append(
            {
                "field": field,
                "op": op,
                "value": raw.get("value"),
                "label": str(raw.get("label", ""))[:64],
            }
        )
    return normalized


def _as_number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _match_one(cond: dict, facts: dict) -> bool:
    field, op, expected = cond.get("field"), cond.get("op"), cond.get("value")
    if field not in facts:
        # 缺字段判不命中，见模块文档；exists 是例外，它问的就是"有没有"
        return op == "exists" and expected is False
    actual = facts[field]
    if op == "exists":
        present = actual not in (None, "", [], {})
        return present if expected is not False else not present
    if actual is None:
        return False

    if op in (">", ">=", "<", "<=", "between"):
        left, right = _as_number(actual), None
        if left is None:
            return False
        if op == "between":
            low, high = _as_number(expected[0]), _as_number(expected[1])
            if low is None or high is None:
                return False
            return low <= left <= high
        right = _as_number(expected)
        if right is None:
            return False
        return {
            ">": left > right,
            ">=": left >= right,
            "<": left < right,
            "<=": left <= right,
        }[op]

    if op in ("in", "not_in"):
        pool = expected if isinstance(expected, (list, tuple, set)) else [expected]
        values = actual if isinstance(actual, (list, tuple, set)) else [actual]
        hit = any(v in pool for v in values)
        return hit if op == "in" else not hit

    if op == "contains":
        haystack = actual if isinstance(actual, (list, tuple, set)) else [actual]
        return any(str(expected) in str(v) for v in haystack)

    if op == "==":
        return str(actual) == str(expected)
    if op == "!=":
        return str(actual) != str(expected)
    return False


def evaluate(conditions: list[dict], facts: dict, mode: str = "all") -> tuple[bool, list[dict]]:
    """对一组条件求值，返回 (是否命中, 命中的条件明细)。

    `mode="all"` 全部满足才算命中（纳入规则的常见口径：诊断 + 指标同时满足）；
    `mode="any"` 任一满足即命中（排除规则与转诊触发的常见口径：任一红线即触发）。

    命中明细一并返回，是因为业务上每一处都要求"给出依据"——目标池要显示
    `matched_rules`、转诊单要显示 `trigger_evidence`、筛查要显示判定理由。
    先算完再回头拼理由，就会拼出与判定不一致的那种最难查的 bug。
    """
    if not conditions:
        # 空规则不命中任何人。这条口径很重要：新建病种时规则是空的，
        # 若空规则视为"全命中"，一次自动识别就会把全县人口纳进目标池。
        return False, []
    matched = [c for c in conditions if _match_one(c, facts)]
    hit = len(matched) == len(conditions) if mode == "all" else bool(matched)
    return hit, matched


def screen(include: list[dict], exclude: list[dict], facts: dict) -> dict:
    """专病人群判定：命中纳入且未命中排除 → 目标人群。

    返回 `{"result": suspect|excluded|normal, "matched": [...], "excluded_by": [...]}`。
    排除优先于纳入——"有禁忌"压过"符合适应证"，这在临床上没有争议。
    """
    ex_hit, ex_matched = evaluate(exclude, facts, mode="any")
    if ex_hit:
        return {"result": "excluded", "matched": [], "excluded_by": ex_matched}
    in_hit, in_matched = evaluate(include, facts, mode="all")
    if in_hit:
        return {"result": "suspect", "matched": in_matched, "excluded_by": []}
    return {"result": "normal", "matched": [], "excluded_by": []}


def judge_level(value: float | None, low: float | None, high: float | None) -> str:
    """按管理目标区间判定单个指标等级：normal / high / low。

    上下限都为空表示"没设目标"，一律记 normal——不设目标却报异常，
    等于把没配置说成有问题。
    """
    number = _as_number(value)
    if number is None:
        return "normal"
    if high is not None and number > float(high):
        return "high"
    if low is not None and number < float(low):
        return "low"
    return "normal"


def score_scale(items: list[dict], answers: dict, scoring: dict) -> dict:
    """量表评分：按题目选项分值累加，再落到 scoring.ranges 给出风险等级与建议。

    题型只认三种：single（单选，取选中项分值）、multi（多选，累加）、
    number（数值题，配 `score_per_unit` 时按值折算，否则不计分）。
    未作答的题按 0 分计入，并在返回里给出 `answered` / `total_items`——
    做了一半的量表不该看起来和"全选最低分"一样。
    """
    total = 0.0
    answered = 0
    for item in items or []:
        key = item.get("key")
        if key is None or key not in answers:
            continue
        answered += 1
        value = answers[key]
        item_type = item.get("type", "single")
        options = {str(o.get("label")): o.get("score", 0) for o in item.get("options", [])}
        if item_type == "multi":
            for one in value if isinstance(value, list) else [value]:
                total += float(options.get(str(one), 0) or 0)
        elif item_type == "number":
            per = _as_number(item.get("score_per_unit"))
            number = _as_number(value)
            if per is not None and number is not None:
                total += per * number
        else:
            total += float(options.get(str(value), 0) or 0)

    risk, advice = "", ""
    for rng in (scoring or {}).get("ranges", []):
        low, high = _as_number(rng.get("min")), _as_number(rng.get("max"))
        if (low is None or total >= low) and (high is None or total <= high):
            risk, advice = rng.get("risk", ""), rng.get("advice", "")
            break
    return {
        "score": round(total, 2),
        "risk_level": risk,
        "advice": advice,
        "answered": answered,
        "total_items": len(items or []),
    }


def grade_abnormal(rules: list[dict], answers: dict) -> tuple[str, str]:
    """随访问卷异常分级：返回 (级别, 处置措施)，取命中的最高级别。

    级别序 none < low < mid < high。命中多条时取最重的那条，
    而不是最后一条——规则的书写顺序不该决定病人的分级。
    """
    order = {"none": 0, "low": 1, "mid": 2, "high": 3}
    best_level, best_action = "none", ""
    for rule in rules or []:
        cond = rule.get("when") or {}
        if not cond:
            continue
        hit, _ = evaluate([cond], answers, mode="all")
        if not hit:
            continue
        level = rule.get("level", "low")
        if order.get(level, 0) > order.get(best_level, 0):
            best_level, best_action = level, rule.get("action", "")
    return best_level, best_action
