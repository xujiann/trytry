"""宽字典配置里的数收 NaN / Infinity：比较永不成立、计分出参 500、`int(inf)` 500、PG 存不进（P2-466）。

由 P2-465（慢病分级阈值）一处引出，按形状扫一遍所有登记为配置的宽字典（`test_config_dict_validated.CONFIG`）：
标准库 `json.loads` 照收 `NaN` / `Infinity` 记号，P1-92 的 `FiniteFloat` 管不到字典里面，各个配置校验只查
「是不是数」——NaN / Infinity 也是 float。2026-09-27 实测：

- 质控区间的界、筛查 / 分组 / 转诊条件的比较值写成 NaN：存进去之后这条规则永远判不出、条件永远不命中，没有任何报错；
- 考核指标的满分、方案权重写成 Infinity：建 201，一计分整张方案的出参编码失败（500）；
- 服务包的次数写成 Infinity：校验里 `int(inf)` 抛 OverflowError，不在接住的两种里，建服务包即 500；
- 以上在 PG 上全都更早炸：JSON 列存不进 NaN，建配置即 500。

修法：共用一个 `numtypes.non_finite_path`，每个配置校验开头过一遍（闸门在 `test_config_dict_validated.py`）；
考核计分的 `_is_number` 同时不认非有限的数，存量里的坏规则逐指标记错、不 500。
"""
import pytest
from fastapi import HTTPException

NAN, INF = float("nan"), float("inf")


def test_路径说得出是哪一个数():
    from app.numtypes import non_finite_path

    assert non_finite_path({"metrics": [{"key": "sbp", "level3": NAN}]}, "level_rules") == "level_rules.metrics[0].level3"
    assert non_finite_path([{"config": {"t": [1, -INF]}}], "steps") == "steps[0].config.t[1]"
    assert non_finite_path(INF, "value") == "value"
    assert non_finite_path({"a": 1, "b": [2.5, "x", None, True, 10**400]}, "c") == ""


def test_慢病分级规则_阈值之外的键也查():
    from app.routers.chronic import level_rules_problem

    assert level_rules_problem({"备注": NAN, "metrics": []}) == "level_rules.备注 不能是 NaN / Infinity"


def test_质控规则_区间的界():
    from app.routers.dataquality import rule_config_problem

    problem = rule_config_problem("patients", "range", {"field": "id", "min": NAN})
    assert problem == "config.min 不能是 NaN / Infinity"


def test_集成流程步骤():
    from app.routers.esb import _validate_steps

    with pytest.raises(HTTPException) as caught:
        _validate_steps([{"type": "transform", "config": {"scale": INF}}])
    assert caught.value.status_code == 422 and caught.value.detail == "steps[0].config.scale 不能是 NaN / Infinity"


def test_考核评分规则与方案权重():
    from app.spd.routers.assess import _check_plan_items, plan_weight_problem, score_rule_problem

    assert score_rule_problem({"type": "ratio", "full": INF}) == "score_rule.full 不能是 NaN / Infinity"
    assert score_rule_problem({"type": "step", "steps": [{"min": 0, "max": NAN, "score": 5}]}) == (
        "score_rule.steps[0].max 不能是 NaN / Infinity")
    # 计分时对存量里的坏权重逐指标记错（`_is_number` 不再认非有限的数），不让它算进分数
    assert plan_weight_problem([{"indicator_code": "k1", "weight": INF}]) == (
        "考核方案里指标 k1 的权重必须是不小于 0 的数")
    with pytest.raises(HTTPException) as caught:
        _check_plan_items(None, [{"indicator_code": "k1", "weight": NAN}])   # 查库之前先挡
    assert caught.value.detail == "考核方案的 items[0].weight 不能是 NaN / Infinity"


@pytest.mark.parametrize("value", [NAN, [0, INF]])
def test_筛查分组转诊条件的比较值(value):
    from app.spd.rules import RuleError, validate_conditions

    op = "between" if isinstance(value, list) else ">="
    with pytest.raises(RuleError, match="不能是 NaN / Infinity"):
        validate_conditions([{"field": "age", "op": op, "value": value}])


def test_量表选项分值与评分分段():
    from app.spd.rules import scale_problem

    assert scale_problem([{"key": "q1", "options": [{"label": "是", "score": NAN}]}], {}) == (
        "items[0].options[0].score 不能是 NaN / Infinity")
    assert scale_problem([], {"ranges": [{"min": 0, "max": INF, "risk": "high"}]}) == (
        "scoring.ranges[0].max 不能是 NaN / Infinity")


def test_服务包次数():
    from app.spd.service import package_items_ok

    assert package_items_ok([{"code": "BP", "times": INF}]) is False   # 修前 int(inf) 抛 OverflowError
    assert package_items_ok([{"code": "BP", "times": 4}]) is True


def test_随访问卷题目与异常规则():
    from app.spd.routers.followup import _check_abnormal_rules

    with pytest.raises(HTTPException) as caught:
        _check_abnormal_rules([], [{"key": "pain", "max": INF}])
    assert caught.value.detail == "items[0].max 不能是 NaN / Infinity"


def test_接口端_服务包次数写成Infinity是422不是500(client, admin):
    body = '{"code": "p2466_pkg", "name": "P2466 服务包", "items": [{"code": "BP", "times": Infinity}]}'
    resp = client.post("/api/spd/service-packages", headers={**admin, "Content-Type": "application/json"},
                       content=body.encode())
    assert resp.status_code == 422, resp.text[:300]   # 修前 500（OverflowError）
    assert resp.json() == {"detail": "服务包项目须有编码且次数大于0"}


def test_接口端_考核指标满分写成Infinity是422(client, admin):
    body = '{"code": "P2466-IND", "name": "P2466 指标", "score_rule": {"type": "ratio", "full": Infinity}}'
    resp = client.post("/api/spd/indicators", headers={**admin, "Content-Type": "application/json"},
                       content=body.encode())
    assert resp.status_code == 422, resp.text[:300]   # 修前 201，之后整张方案一计分就 500
    assert resp.json() == {"detail": "评分规则非法：score_rule.full 不能是 NaN / Infinity"}
