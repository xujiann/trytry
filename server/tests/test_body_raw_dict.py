"""写接口的请求体不得是裸 dict：建档有模型、改档收裸 dict，建档的校验在改档上全部失效（P1-94）。

2026-09-24 实测 24 处：慢专病配置改档 20 个（`PATCH …` 收 `body: dict`，再按一份键清单 `setattr`）+
对接 FHIR 入站 4 个。前 20 个里任何类型、长度、取值范围、业务校验都不做，写坏的配置在下游炸开
（开发库 SQLite 上实测，修前）：

- **整张列表永久 500**：问卷 `abnormal_rules` 传个字符串、报告模板 `sections` 传个字符串、推送任务
  `priority` 传 `"high"`——PATCH 先 `commit` 再做出参校验，本次 500 的同时坏值**已经落库**，此后
  `GET /questionnaires`、`/report-templates`、`/report-tasks` 对所有人都是 500，直到有人手工改库；
- **下游流程 500**：随访方案时间点改成 `[99999999]` → 200 照存，此后「按方案生成随访」500（日期溢出）；
  管理目标随访周期改成 99999999 → 此后该病种每完成一次随访任务都 500（算下次随访日溢出）；
- **静默失效**：随访方案时间点改成 `[]` → 200，此后按方案生成随访 201、`created=0`，一条也不生成；
- **不可空的列传 null** → 500（`IntegrityError`）；**超长字符串**在开发库照存、生产库 500。

修法：20 个端点各换成 `*Patch` 模型——字段与约束照抄建档模型：不传即不改（`model_dump(exclude_unset=True)`）；
不可空的列声明成 `T`、默认值取 `app/patchtypes.UNSET`，显式传 null 是 422；建档时的业务校验（时间点范围、
异常分级规则、量表题目 key 唯一、报告模板至少一段、转诊条件至少一条、外键先查存在）改档同一句，
能抽的抽成同一个函数。原先键清单之外的键照旧忽略（pydantic 默认 `extra="ignore"`）。

判据用 FastAPI 自己解析出的请求体参数（`dependant.body_params`），不读源码：`dict`、`dict[str, Any]`、
`Any`、`list[dict]` 算裸；`list[某模型]`、`list[str]` 不算（元素有类型）。上传文件 / 表单字段不是 JSON
请求体，类型也不是裸的，自然不在此列。
"""
import types
import typing
from collections.abc import Mapping

import pytest
import test_api_contract_governance as contract
from fastapi import APIRouter, Body, UploadFile
from pydantic import BaseModel

#: 基线：已清零，此后即零基线闸门（`scripts/dump_gate_status.py` 把它列进闸门现状）。
#: 2026-09-24 实测 24 处裸 dict 请求体：慢专病配置改档 20 个（本批全部换成 `*Patch` 模型）+ 下面 4 个按设计。
BASELINE = 0

#: 按设计收原样资源的入站端点——只减不增，每条写明理由。
BY_DESIGN: dict[str, str] = {
    key: (
        "FHIR R4 资源按标准原样入站：结构由 FHIR 规范定义、层层可选（identifier / component / basedOn / "
        "reasonCode…），由 `parse_fhir_*` / `_do_fhir_*` 逐字段取值、缺什么回什么 422，意外结构由 "
        "`_run_inbound` 兜成 422 并落交换日志。换成 pydantic 模型等于在这里再写一份 FHIR 资源模型，是另一件事。"
    )
    for key in (
        "integration:fhir_patient",
        "integration:fhir_observation",
        "integration:fhir_diagnostic_report",
        "integration:fhir_encounter",
    )
}

_UNION = (typing.Union, types.UnionType)
_CONTAINERS = (list, set, frozenset, tuple)


# ================================================================ 判据
def _untyped(tp) -> bool:
    """`tp` 是不是「照单全收」的类型：dict / Mapping / Any / object，或元素是它们的容器。"""
    if typing.get_origin(tp) is typing.Annotated:
        return _untyped(typing.get_args(tp)[0])
    if tp is typing.Any or tp is object:
        return True
    origin = typing.get_origin(tp) or tp
    if origin in _UNION:
        return any(_untyped(a) for a in typing.get_args(tp) if a is not type(None))
    if isinstance(origin, type) and issubclass(origin, (dict, Mapping)):
        return True
    if origin in _CONTAINERS:
        args = [a for a in typing.get_args(tp) if a is not Ellipsis]
        return not args or any(_untyped(a) for a in args)
    return False


def _dependants(dep):
    yield dep
    for sub in dep.dependencies:
        yield from _dependants(sub)


def raw_bodies(endpoints=None) -> list[str]:
    """`模块:端点函数`：请求体（含依赖里声明的）是裸 dict 一类的写接口。"""
    endpoints = contract._iter_endpoints() if endpoints is None else endpoints
    return sorted({
        f"{name}:{route.endpoint.__name__}"
        for name, route in endpoints
        for dep in _dependants(route.dependant)
        for param in dep.body_params
        if _untyped(param.field_info.annotation)
    })


# ================================================================ 闸门
def test_写接口的请求体不得是裸dict():
    offenders = [key for key in raw_bodies() if key not in BY_DESIGN]
    assert len(offenders) <= BASELINE, (
        "请求体收裸 dict 的写接口（建档模型的一切校验在这里都不生效，见本文件 docstring）——"
        "请声明请求模型；改档照 `*Patch` 的写法：字段与约束照抄建档模型、默认 None、"
        f"`model_dump(exclude_unset=True)`：{offenders}"
    )


def test_按设计名单只许变少_且条条都还在():
    current = set(raw_bodies())
    stale = sorted(set(BY_DESIGN) - current)
    assert not stale, f"这些端点已不再收裸 dict，从 BY_DESIGN 里划掉：{stale}"
    for key, reason in BY_DESIGN.items():
        assert len(reason) >= 20, f"{key} 的豁免理由写得太短"


def test_判据自证_裸的都点名_有类型的不报():
    class Item(BaseModel):
        name: str

    router = APIRouter()

    @router.patch("/a")
    def bare_dict(body: dict):
        return {}

    @router.patch("/b")
    def dict_any(body: dict[str, typing.Any]):
        return {}

    @router.post("/c")
    def list_of_dict(body: list[dict]):
        return {}

    @router.post("/d")
    def optional_dict(body: dict | None = None):
        return {}

    @router.post("/e")
    def any_body(body: typing.Any = Body()):   # 不写 Body() 时 FastAPI 把 Any 当查询参数
        return {}

    @router.patch("/f")
    def typed(body: Item):
        return {}

    @router.post("/g")
    def list_of_model(body: list[Item]):
        return {}

    @router.post("/h")
    def list_of_str(body: list[str]):
        return {}

    @router.post("/i")
    def upload(file: UploadFile):
        return {}

    routes = [("自证", r) for r in router.routes]
    assert raw_bodies(routes) == [
        "自证:any_body", "自证:bare_dict", "自证:dict_any", "自证:list_of_dict", "自证:optional_dict",
    ]


# ================================================================ 行为回归（修前实测见 docstring）
B = "/api/spd"


def test_改随访方案_时间点越界或清空是422_按方案生成照常(client, admin):
    rule = client.post(f"{B}/followup-rules", headers=admin,
                       json={"code": "P194-FR", "name": "改档回归方案", "points": [7, 30]})
    assert rule.status_code == 201, rule.text
    rid = rule.json()["id"]
    for points, word in (([99999999], "0~3650"), ([], "至少要有一个")):
        resp = client.patch(f"{B}/followup-rules/{rid}", headers=admin, json={"points": points})
        assert resp.status_code == 422 and word in resp.json()["detail"], resp.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "改档回归", "id_card": "110101199001019473", "gender": "男",
        "birth_date": "1990-01-01", "phone": "13800009473"})
    assert patient.status_code == 201, patient.text
    plan = client.post(f"{B}/followup-plans", headers=admin,
                       json={"patient_id": patient.json()["id"], "rule_id": rid})
    assert plan.status_code == 201 and plan.json()["created"] == 2, plan.text


def test_改档不传即不改_不可空的列传null或超长是422(client, admin):
    rule = client.post(f"{B}/followup-rules", headers=admin,
                       json={"code": "P194-NULL", "name": "改档空值", "points": [14]})
    rid = rule.json()["id"]
    for bad in ({"name": None}, {"name": "长" * 65}, {"active": None}, {"points": None}):
        resp = client.patch(f"{B}/followup-rules/{rid}", headers=admin, json=bad)
        assert resp.status_code == 422, (bad, resp.text)
    resp = client.patch(f"{B}/followup-rules/{rid}", headers=admin, json={"active": False, "code": "改不了"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["active"], body["name"], body["points"], body["code"]) == (False, "改档空值", [14], "P194-NULL")


def test_改问卷_异常规则写坏是422_问卷列表照常(client, admin):
    q = client.post(f"{B}/questionnaires", headers=admin,
                    json={"code": "P194-Q", "name": "改档问卷", "items": [{"key": "a", "label": "A"}]})
    assert q.status_code == 201, q.text
    qid = q.json()["id"]
    bad_op = [{"when": {"field": "a", "op": "bogus", "value": 1}, "level": "high"}]
    for bad in ({"abnormal_rules": "not-a-list"}, {"abnormal_rules": bad_op}, {"items": {"a": 1}}):
        resp = client.patch(f"{B}/questionnaires/{qid}", headers=admin, json=bad)
        assert resp.status_code == 422, (bad, resp.text)
    assert client.get(f"{B}/questionnaires", headers=admin).status_code == 200


def test_改报告模板与推送任务_类型不对是422_两张列表照常(client, admin):
    tpl = client.post(f"{B}/report-templates", headers=admin, json={
        "code": "P194-T", "name": "改档模板", "sections": [{"type": "kpi", "title": "在管"}]})
    assert tpl.status_code == 201, tpl.text
    tid = tpl.json()["id"]
    for bad in ({"sections": "abc"}, {"sections": []}, {"period": "yearly"}, {"variables": [1]}):
        resp = client.patch(f"{B}/report-templates/{tid}", headers=admin, json=bad)
        assert resp.status_code == 422, (bad, resp.text)
    task = client.post(f"{B}/report-tasks", headers=admin, json={"template_id": tid, "name": "改档推送"})
    assert task.status_code == 201, task.text
    kid = task.json()["id"]
    for bad in ({"priority": "high"}, {"priority": 0}, {"valid_to": "明天"}, {"status": "bogus"},
                {"subscriber_ids": "1,2"}, {"push_time": "08:00:00"}):
        resp = client.patch(f"{B}/report-tasks/{kid}", headers=admin, json=bad)
        assert resp.status_code == 422, (bad, resp.text)
    paused = client.patch(f"{B}/report-tasks/{kid}", headers=admin, json={"status": "paused"})
    assert paused.status_code == 200 and paused.json()["status"] == "paused", paused.text
    assert client.get(f"{B}/report-templates", headers=admin).status_code == 200
    assert client.get(f"{B}/report-tasks", headers=admin).status_code == 200


def test_改转诊规则_目标机构不存在是404_条件清空是422(client, admin):
    rule = client.post(f"{B}/referral-rules", headers=admin, json={
        "code": "P194-RR", "name": "改档转诊", "conditions": [{"field": "sbp", "op": ">=", "value": 180}]})
    assert rule.status_code == 201, rule.text
    rid = rule.json()["id"]
    missing = client.patch(f"{B}/referral-rules/{rid}", headers=admin, json={"target_org_id": 999999})
    assert missing.status_code == 404 and "999999" in missing.json()["detail"], missing.text
    empty = client.patch(f"{B}/referral-rules/{rid}", headers=admin, json={"conditions": []})
    assert empty.status_code == 422 and "至少要有一个" in empty.json()["detail"], empty.text
    for bad in ({"handle_level": "moon"}, {"auto_task": "maybe"}):
        resp = client.patch(f"{B}/referral-rules/{rid}", headers=admin, json=bad)
        assert resp.status_code == 422, (bad, resp.text)
    ok = client.patch(f"{B}/referral-rules/{rid}", headers=admin, json={"handle_level": "county"})
    assert ok.status_code == 200 and ok.json()["conditions"] == rule.json()["conditions"], ok.text


def test_改上报任务_负责人不存在是404(client, admin):
    task = client.post(f"{B}/case-report-tasks", headers=admin, json={"code": "P194-CRT", "name": "改档上报"})
    assert task.status_code == 201, task.text
    resp = client.patch(f"{B}/case-report-tasks/{task.json()['id']}", headers=admin,
                        json={"manager_user_id": 999999})
    assert resp.status_code == 404 and "999999" in resp.json()["detail"], resp.text


@pytest.fixture(scope="module")
def p194_target(client, admin):
    program = client.get(f"{B}/programs", headers=admin).json()[0]
    target = client.post(f"{B}/programs/{program['id']}/targets", headers=admin, json={
        "metric": "p194_metric", "metric_name": "改档指标", "target_high": 140})
    assert target.status_code == 201, target.text
    return target.json()


@pytest.mark.parametrize("bad", [
    {"followup_interval_days": 99999999},   # 修前照存，此后该病种每完成一次随访任务都 500
    {"followup_interval_days": 0},
    {"target_low": "NaN"},
    {"unit": "x" * 17},
    {"target_low": 150},                     # 下限高过存量的上限 140——建档那句，改档同样拦
    {"target_high": None},                   # 量化目标上下限都清空
])
def test_改管理目标_越界是422_目标不变(client, admin, p194_target, bad):
    resp = client.patch(f"{B}/targets/{p194_target['id']}", headers=admin, json=bad)
    assert resp.status_code == 422, (bad, resp.text)
    program_targets = client.get(f"{B}/programs/{p194_target['program_id']}/targets", headers=admin).json()
    assert [t for t in program_targets if t["id"] == p194_target["id"]] == [p194_target]


def test_改考核方案_指标清空或不存在是422_建方案条目缺编码是422而不是500(client, admin):
    ind = client.post(f"{B}/indicators", headers=admin, json={"code": "P194-IND", "name": "改档指标"})
    assert ind.status_code == 201, ind.text
    plan = client.post(f"{B}/assess-plans", headers=admin, json={
        "code": "P194-PLAN", "name": "改档方案", "items": [{"indicator_code": "P194-IND", "weight": 1}]})
    assert plan.status_code == 201, plan.text
    pid = plan.json()["id"]
    for items, word in (([], "至少要有一个"), ([{"indicator_code": "P194-NOPE"}], "P194-NOPE"),
                        ([{"weight": 1}], "indicator_code")):
        resp = client.patch(f"{B}/assess-plans/{pid}", headers=admin, json={"items": items})
        assert resp.status_code == 422 and word in resp.json()["detail"], (items, resp.text)
    # 建方案同一句：条目缺编码 / 编码是数字，原先拼报错时 `'、'.join` 撞上 None / 整数，422 成了 500
    for n, items in enumerate(([{"weight": 1}], [{"indicator_code": 5}])):
        resp = client.post(f"{B}/assess-plans", headers=admin,
                           json={"code": f"P194-PLAN{n}", "name": "条目缺编码", "items": items})
        assert resp.status_code == 422, (items, resp.text)
    assert client.get(f"{B}/assess-plans", headers=admin).status_code == 200


def test_改量表_题目key重复是422_与建量表同一句(client, admin):
    scale = client.post(f"{B}/scales", headers=admin, json={
        "code": "P194-SC", "name": "改档量表", "items": [{"key": "a"}, {"key": "b"}]})
    assert scale.status_code == 201, scale.text
    resp = client.patch(f"{B}/scales/{scale.json()['id']}", headers=admin,
                        json={"items": [{"key": "a"}, {"key": "a"}]})
    assert resp.status_code == 422 and "key 不得重复" in resp.json()["detail"], resp.text
