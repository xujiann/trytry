"""宽字典请求体字段：配置写库之前得查结构（P1-123 / P2-79 / P2-80 / P1-125 / P2-81 / P2-82 一族的闸门）。

请求模型里注解成 `dict` / `list[dict]` 的字段，pydantic 只管「是个对象 / 对象列表」，里面长什么样一概不查。
这类字段有两种：一种是**配置**——存下来、之后被反复求值（分组规则、评分规则、量表、分级规则、质控规则、
服务包项目……）；一种是**数据**——当次用一下，或原样存原样回显（作答、材料、试算事实）。

配置写坏了的代价在「用的时候」：2026-09-25 一天量出六处，写坏的配置照样 201，之后按规则入组 / 考核计分 /
作答 / 记随访 / 质控扫描 / 绑定服务包就 500——而且常常是**整批**一起 500（一张方案、一个看板、一整轮推送）。

**闸门**（派生、零基线）：
- 配置字段登记在 `CONFIG`，写明它的结构校验函数；收这个模型的每个写接口，函数连同两层同模块调用里都得出现它；
- 数据字段登记在 `DATA`，逐条写明为什么不用查结构；
- 请求模型里新出现的宽字典字段，两边都没登记即红——建的时候就得说清楚它是配置还是数据。
"""
import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
ROUTE_DIRS = (APP / "routers", APP / "spd" / "routers")
#: 「宽」的注解：里面长什么样 pydantic 不查（`dict[str, float]` 这类值有类型的不算）
WIDE = {"dict", "list[dict]", "dict | None", "list[dict] | None", "dict[str, Any]", "list[dict[str, Any]]",
        "dict[str, Any] | None", "list[dict[str, Any]] | None"}
BASELINE = 0

#: 配置字段 → 结构校验的记号（写接口连同两层同模块调用里出现任一即算查过）
CONFIG = {
    "routers/chronic.py:DiseaseTypeCreate.level_rules": ("level_rules_problem(",),
    "routers/chronic.py:DiseaseTypeUpdate.level_rules": ("level_rules_problem(",),
    "routers/dataquality.py:RuleCreate.config": ("rule_config_problem(",),
    "routers/dataquality.py:RuleUpdate.config": ("rule_config_problem(",),
    "routers/esb.py:FlowCreate.steps": ("_validate_steps(",),
    "routers/esb.py:FlowUpdate.steps": ("_validate_steps(",),
    "spd/routers/assess.py:IndicatorIn.score_rule": ("score_rule_problem(",),
    "spd/routers/assess.py:IndicatorPatch.score_rule": ("score_rule_problem(",),
    "spd/routers/assess.py:PlanIn.items": ("_check_plan_items(",),
    "spd/routers/assess.py:PlanPatch.items": ("_check_plan_items(",),
    "spd/routers/config/catalog.py:ProgramIn.include_rules": ("_conditions(",),
    "spd/routers/config/catalog.py:ProgramIn.exclude_rules": ("_conditions(",),
    "spd/routers/config/catalog.py:ProgramUpdate.include_rules": ("_conditions(",),
    "spd/routers/config/catalog.py:ProgramUpdate.exclude_rules": ("_conditions(",),
    "spd/routers/config/paths.py:PathNodeIn.enter_condition": ("_conditions(",),
    "spd/routers/config/paths.py:PathNodeIn.complete_condition": ("_conditions(",),
    "spd/routers/config/paths.py:PathNodePatch.enter_condition": ("_conditions(",),
    "spd/routers/config/paths.py:PathNodePatch.complete_condition": ("_conditions(",),
    "spd/routers/config/scales.py:PackageIn.items": ("_check_package_items(",),
    "spd/routers/config/scales.py:PackagePatch.items": ("_check_package_items(",),
    "spd/routers/config/scales.py:ScaleIn.items": ("_check_scale(",),
    "spd/routers/config/scales.py:ScaleIn.scoring": ("_check_scale(",),
    "spd/routers/config/scales.py:ScalePatch.items": ("_check_scale(",),
    "spd/routers/config/scales.py:ScalePatch.scoring": ("_check_scale(",),
    "spd/routers/followup.py:QuestionnaireIn.items": ("_check_abnormal_rules(",),
    "spd/routers/followup.py:QuestionnaireIn.abnormal_rules": ("_check_abnormal_rules(",),
    "spd/routers/followup.py:QuestionnairePatch.items": ("_check_abnormal_rules(",),
    "spd/routers/followup.py:QuestionnairePatch.abnormal_rules": ("_check_abnormal_rules(",),
    "spd/routers/population.py:GroupIn.auto_rule": ("validate_conditions(",),
    "spd/routers/referral.py:ReferralRuleIn.conditions": ("validate_conditions(",),
    "spd/routers/referral.py:ReferralRulePatch.conditions": ("validate_conditions(",),
}

#: 数据字段：不是之后被反复求值的配置——逐条写明理由
_ANSWERS = "作答：当次交给量表 / 问卷求值，求值对任意取值都不抛错（缺字段判不命中），答案原样存"
DATA = {
    "routers/esb.py:MessageIn.payload": "报文：原样投递、原样存档，结构由对接双方约定",
    "routers/rules.py:EvaluateIn.variables": "试算入参：当次求值、不落库",
    "spd/routers/care.py:AssessIn.answers": _ANSWERS,
    "spd/routers/followup.py:ExecuteIn.answers": _ANSWERS,
    "spd/routers/population.py:ScreeningIn.answers": _ANSWERS,
    "spd/routers/portal.py:SelfFollowupIn.answers": _ANSWERS,
    "spd/routers/portal.py:SelfScreenIn.answers": _ANSWERS,
    "spd/routers/config/catalog.py:ProgramIn.stages":
        "阶段清单：pydantic 已限定是对象列表；建档取第一段的 key，缺了按空串，不会抛错",
    "spd/routers/config/catalog.py:ProgramUpdate.stages":
        "阶段清单：pydantic 已限定是对象列表；建档取第一段的 key，缺了按空串，不会抛错",
    "spd/routers/config/catalog.py:ProgramIn.milestones": "里程碑：只存只回显（2026-09-25 没有按它求值的地方）",
    "spd/routers/config/catalog.py:ProgramUpdate.milestones": "里程碑：只存只回显（2026-09-25 没有按它求值的地方）",
    "spd/routers/followup.py:ReportTemplateIn.sections":
        "报告段落：按 key 找取数口径，找不到或参数不对都给一句说明（compose_section），不抛错",
    "spd/routers/followup.py:ReportTemplatePatch.sections":
        "报告段落：按 key 找取数口径，找不到或参数不对都给一句说明（compose_section），不抛错",
    "spd/routers/followup.py:ReportTemplateIn.variables": "模板变量：只存只回显",
    "spd/routers/followup.py:ReportTemplatePatch.variables": "模板变量：只存只回显",
    "spd/routers/population.py:EnrollIn.habits": "生活习惯：档案描述，只存只回显",
    "spd/routers/population.py:EnrollUpdate.habits": "生活习惯：档案描述，只存只回显",
    "spd/routers/portal.py:TaskSubmitIn.result": "任务填报结果：结构由各业务表单自定，原样存",
    "spd/routers/tasks.py:SubmitIn.result": "任务填报结果：结构由各业务表单自定，原样存",
    "spd/routers/referral.py:ReferralIn.materials": "转诊材料：只存只回显",
    "spd/routers/referral.py:ReferralIn.trigger_evidence": "触发依据：只存只回显",
    "spd/routers/referral.py:RuleCheckIn.extra": "试算事实：当次求值、不落库，求值对任意取值都不抛错",
    "spd/routers/tasks.py:StartPathIn.overrides": "路径实例的覆盖项：只存只回显（2026-09-25 没有按它求值的地方）",
    "spd/routers/tasks.py:InstanceAdjustIn.overrides": "路径实例的覆盖项：只存只回显（2026-09-25 没有按它求值的地方）",
}


def _write_handlers(sources: dict[str, str] | None = None):
    """逐个给出写接口：(`文件:模型.字段`, 函数名, 函数连同两层同模块调用的源码)——只看请求体里的宽字典字段。"""
    files = {p.relative_to(APP).as_posix(): p.read_text(encoding="utf-8")
             for base in ROUTE_DIRS for p in sorted(base.rglob("*.py")) if "__pycache__" not in p.parts}
    files.update(sources or {})
    for rel, text in files.items():
        tree = ast.parse(text)
        wide = {n.name: [s.target.id for s in n.body if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
                         and ast.unparse(s.annotation) in WIDE]
                for n in tree.body if isinstance(n, ast.ClassDef)}
        funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

        def closure(fn, depth=2, seen=None) -> str:
            seen = set() if seen is None else seen
            src = ast.unparse(fn)
            if depth:
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                            and node.func.id in funcs and node.func.id not in seen:
                        seen.add(node.func.id)
                        src += "\n" + closure(funcs[node.func.id], depth - 1, seen)
            return src

        for fn in funcs.values():
            if not any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                       and d.func.attr in ("post", "put", "patch") for d in fn.decorator_list):
                continue
            for arg in fn.args.args:
                model = ast.unparse(arg.annotation) if arg.annotation is not None else ""
                for field in wide.get(model, []):
                    yield f"{rel}:{model}.{field}", fn.name, closure(fn)


def unchecked_configs(sources: dict[str, str] | None = None) -> list[str]:
    """`字段 @ 写接口`：登记为配置、写接口里却没有它的结构校验。"""
    return sorted(f"{key} @ {fn}" for key, fn, src in _write_handlers(sources)
                  if key in CONFIG and not any(marker in src for marker in CONFIG[key]))


def test_配置型宽字典写库之前得查结构():
    bad = unchecked_configs()
    assert len(bad) <= BASELINE, (
        "以下写接口收配置型宽字典、却没调它的结构校验：\n  " + "\n  ".join(bad)
        + "\n\n写坏的配置照样落库，之后求值时 500（常常是整批一起）。写库前查结构，422。"
    )


def test_宽字典字段都判过_配置还是数据():
    seen = {key for key, _fn, _src in _write_handlers()}
    unjudged = seen - set(CONFIG) - set(DATA)
    assert not unjudged, (
        f"请求体里新出现的宽字典字段 {sorted(unjudged)}：之后会被反复求值的是配置，登记进 CONFIG 并在写库前查结构；"
        "当次用一下或原样存的是数据，写明理由登记进 DATA。"
    )
    assert not set(CONFIG) & set(DATA), sorted(set(CONFIG) & set(DATA))
    assert (set(CONFIG) | set(DATA)) <= seen, sorted((set(CONFIG) | set(DATA)) - seen)   # 名单不留死条目
    assert all(reason.strip() for reason in DATA.values())


def test_判据自证_没查的点名_查过的与数据字段不报():
    snippet = (
        "class RuleIn(BaseModel):\n    rules: list[dict] = []\n    note: dict = {}\n"
        "def _check(rules):\n    return validate_rules(rules)\n"
        "@router.post('/a')\ndef bare(body: RuleIn):\n    db.add(X(**body.model_dump()))\n"
        "@router.patch('/b')\ndef checked(body: RuleIn):\n    _check(body.rules)\n"
    )
    global CONFIG, DATA
    saved = CONFIG, DATA
    CONFIG = {**CONFIG, "probe.py:RuleIn.rules": ("validate_rules(",)}
    DATA = {**DATA, "probe.py:RuleIn.note": "探针：数据字段"}
    try:
        assert [f for f in unchecked_configs({"probe.py": snippet}) if f.startswith("probe.py")] == [
            "probe.py:RuleIn.rules @ bare"]
    finally:
        CONFIG, DATA = saved
