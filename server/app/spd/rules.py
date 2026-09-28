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
| `score` | 筛查登记所用量表的得分（只在筛查登记判纳入时有，P2-368） |
| 其余键 | 筛查登记的问卷答案，**只补上面推不出来的**（同名的以库里为准，P2-368） |

缺字段一律判为**不命中**而不是报错：规则是各县自己配的，配了一个本地没采集的
指标不该让整批筛查中断——不命中会体现在筛查结果里，报错只会体现在日志里。
"""
from __future__ import annotations

import math
from typing import Any

from ..numtypes import non_finite_path

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
        # 比较值写成 NaN / Infinity：和谁比都不成立，这条条件永远不命中，也没有任何报错（P2-466）
        bad = non_finite_path(raw.get("value"), f"条件 {field} 的 value")
        if bad:
            raise RuleError(f"{bad} 不能是 NaN / Infinity")
        _check_comparison_value(field, op, raw.get("value"))
        normalized.append(
            {
                "field": field,
                "op": op,
                "value": raw.get("value"),
                "label": str(raw.get("label", ""))[:64],
            }
        )
    return normalized


def _check_comparison_value(field: str, op: str, value) -> None:
    """大小比较与「介于」的比较值读得成数、区间下限不大于上限（P2-712）；NaN / Infinity 由调用方先按 P2-466 报。

    求值按数比、读不成数判不命中，区间按 `low <= 值 <= high` 比——「介于 200,160」「180,」（页面把空段读成 0）
    「180，abc」（读成 null）、「≥ 18O」（字母 O）都建得成，之后永远不命中，也没有任何报错。读得成数的文本（"180"）
    照收，与求值同一个读法。
    """
    if op == "between":
        low, high = _finite_number(value[0]), _finite_number(value[1])
        if low is None or high is None:
            raise RuleError(f"条件 {field} 的区间两端都必须是数（收到 {value}）")
        if low > high:
            raise RuleError(f"条件 {field} 的区间下限 {value[0]} 大于上限 {value[1]}，永远不会命中")
    elif op in (">", ">=", "<", "<=") and _finite_number(value) is None:
        raise RuleError(f"条件 {field} 的比较值必须是数（收到 {value!r}）")


def as_validated(conditions: list[dict]) -> list[dict]:
    """校验一组条件，返回**求值时真正会比的样子**：field / op 换成去掉两端空格后的值，其余键原样（P2-290）。

    `validate_conditions` 按去掉空格后的 field / op 判合法，求值（`_match_one`）却按存进去的原样比——只查不改写的
    调用方（问卷异常规则、分组自动规则）把「pain 」「 >=」照原样存下，规则过了校验、永远不命中。与存整条规范化结果
    （多出 value / label 键）不同，这里只动这两个键：干净的输入存进去的字节不变。
    """
    checked = validate_conditions(conditions)
    return [{**raw, "field": cond["field"], "op": cond["op"]} for raw, cond in zip(conditions or [], checked)]


def _as_number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _finite_number(value) -> float | None:
    """配置时查比较值用：读得成**有限**的数才算（求值同样按 `_as_number` 读；NaN / Infinity 和谁比都不成立）。"""
    number = _as_number(value)
    return number if number is not None and math.isfinite(number) else None


def _same(actual, expected) -> bool:
    """`==` / `!=` / `in` / `not_in` 共用的等值口径（P2-342）：两边都读得成数就按数比，否则按去掉首尾空白的文本比。

    原先 `==` 比 `str()`、`in` 比 Python 的 `in`，两种口径各算各的：监测值是 float，`bp_sys == 140` 遇 140.0 不命中；
    规则编辑器把「属于」的值存成文本列表、「等于」的值能读成数就存成数，`age in ["60", "65"]` 遇 60 岁不命中，
    问卷 `pain in ["8", "9", "10"]` 遇作答 8 判「无异常」、不派处置任务。
    """
    left, right = _as_number(actual), _as_number(expected)
    if left is not None and right is not None:
        return left == right
    return str(actual).strip() == str(expected).strip()


def _match_one(cond: dict, facts: dict) -> bool:
    field, op = cond.get("field"), cond.get("op")
    expected: Any = cond.get("value")  # between 时是二元序列，其余是标量
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

    if op in ("in", "not_in", "==", "!="):
        # 等于 / 不等于就是只有一个值的属于 / 不属于：左值是列表（诊断、多选题作答）时任一元素相等即算——原先 `==`
        # 比的是整个列表的 `str()`，多选作答 ["无"] 配 `症状 != "无"` 恒命中，误判异常、派出处置任务（P2-342）
        if op in ("in", "not_in"):
            pool = expected if isinstance(expected, (list, tuple, set)) else [expected]
        else:
            pool = [expected]
        values = actual if isinstance(actual, (list, tuple, set)) else [actual]
        hit = any(_same(v, p) for v in values for p in pool)
        return hit if op in ("in", "==") else not hit

    if op == "contains":
        haystack = actual if isinstance(actual, (list, tuple, set)) else [actual]
        return any(str(expected) in str(v) for v in haystack)

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


def scale_problem(items: list, scoring: dict) -> str:
    """量表配置里会让 `score_scale` 抛错、或题目根本计不进分的写法，没问题返回空串（P2-80）。

    建 / 改 / 发布量表时拦；作答时对存量里的坏量表说清楚、不 500。原先照单全收：选项写成字符串、分值写成文字、
    选项或评分分段不是列表，建量表 201、发布 200，谁来作答都 500——居民扫码自查也一样；题目没写 key 的，作答
    永远计不进分。分值只要能读成数就行（`score_scale` 本就按 `float()` 读，写成 "3" 的照常计分）。
    """
    bad = non_finite_path(items, "items") or non_finite_path(scoring, "scoring")   # P2-466
    if bad:
        return f"{bad} 不能是 NaN / Infinity"
    for item in items or []:
        key = item.get("key") if isinstance(item, dict) else None
        if not isinstance(key, str) or not key.strip():
            return "每道题都要有 key"
        options = item.get("options", [])
        if not isinstance(options, list) or not all(isinstance(option, dict) for option in options):
            return f"题目 {key} 的选项要写成 [{{label, score}}] 这样的列表"
        for option in options:
            if _as_number(option.get("score") or 0) is None:
                return f"题目 {key} 的选项分值必须是数：{option.get('score')!r}"
    if not isinstance(scoring or {}, dict):
        return "评分规则（scoring）要写成对象"
    ranges = (scoring or {}).get("ranges", [])
    if not isinstance(ranges, list) or not all(isinstance(rng, dict) for rng in ranges):
        return "评分分段（scoring.ranges）要写成 [{min, max, risk, advice}] 这样的列表"
    for rng in ranges:   # 下限不大于上限（P2-712）：颠倒的一段永远落不进去，落进缺口的得分记「未分级」（P2-689）
        low, high = rng.get("min"), rng.get("max")
        if low is not None and high is not None:
            low_n, high_n = _finite_number(low), _finite_number(high)
            if low_n is None or high_n is None:
                return f"评分分段的上下限必须是数：{low!r} ~ {high!r}"
            if low_n > high_n:
                return f"评分分段的下限 {low} 大于上限 {high}，这一段永远落不进去"
    return ""


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

    risk, advice, banded = "", "", False
    ranges = (scoring or {}).get("ranges", [])
    for rng in ranges:
        low, high = _as_number(rng.get("min")), _as_number(rng.get("max"))
        if (low is None or total >= low) and (high is None or total <= high):
            risk, advice, banded = rng.get("risk", ""), rng.get("advice", ""), True
            break
    if ranges and not banded:
        # 得分没落进任何分段（分段之间有缺口、上限封了顶、选项分值带小数）：就是「未分级」，说出来（P2-689）——
        # 筛查登记与居民自查原先把它补成「低危」，最高分落进缺口也记低危、不进目标池、不提示申请服务
        advice = f"得分 {round(total, 2)} 没有落在量表的任何评分分段里，未能按量表分级，请复核量表的评分分段"
    return {
        "score": round(total, 2),
        "risk_level": risk,
        "advice": advice,
        "answered": answered,
        "total_items": len(items or []),
    }


#: 量表判定"疑似"的风险等级门槛。**只有这一份**——医生录入（population.py）与
#: 居民自查（portal.py）曾各写各的：医生侧要 `high` 才算疑似、居民侧 `mid` 就算，
#: 于是同一个人同一份问卷，自己做是"疑似、可申请服务"，医生代录是"未见异常"。
#: 取 mid+ 而不是 high：种子量表里 mid 档的 advice 已经写着"建议复核 / 建议检测"，
#: 判成"未见异常"与量表自己给的建议自相矛盾。
SUSPECT_RISK_LEVELS = ("mid", "high", "very_high")


def is_suspect_risk(risk_level: str) -> bool:
    """量表风险等级是否达到"疑似"门槛（两个入口共用，别再各判各的）。"""
    return risk_level in SUSPECT_RISK_LEVELS


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
