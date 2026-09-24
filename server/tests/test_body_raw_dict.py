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
import ast
import types
import typing
from collections.abc import Mapping

import pytest
import test_api_contract_governance as contract
import test_body_str_length as bodystr
from fastapi import APIRouter, Body, UploadFile
from pydantic import BaseModel

from app.database import Base

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


# ================================================================ 第二层（P1-95）：可空入参写进不可空列
#: 基线：已清零。2026-09-24 实测「请求体字段收得下 null × 写进的列 NOT NULL」78 处：平台侧 13 个处理函数本就把
#: None 挡掉（`model_dump(exclude_none=True)` / 循环里 `if value is not None` / 先 `if body.x is None: raise`），
#: 慢专病 6 个改档端点 30 个字段照写——显式传 null 即 `NOT NULL` 约束失败、500（病种档案、随访记录、纳管档案、
#: 复诊、干预、路径实例，开发库实测）。同批改成 `app/patchtypes.UNSET` 的写法，显式 null 是 422。
NULLABLE_BASELINE = 0


def _nullable(tp) -> bool:
    if typing.get_origin(tp) is typing.Annotated:
        return _nullable(typing.get_args(tp)[0])
    if tp is type(None) or tp is typing.Any:
        return True
    if typing.get_origin(tp) in _UNION:
        return any(_nullable(a) for a in typing.get_args(tp))
    return False


def _none_skipped(fn, field: str) -> bool:
    """处理函数自己把 None 挡掉了：`model_dump(exclude_none=True)`、改档循环里 `if value is not None:` 包着
    `setattr`、显式赋值包在 `if body.字段 is not None:` 里，或先 `if body.字段 is None: raise`。"""
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "model_dump" \
                and any(k.arg == "exclude_none" and isinstance(k.value, ast.Constant) and k.value.value is True
                        for k in node.keywords):
            return True
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and len(node.test.ops) == 1 \
                and isinstance(node.test.ops[0], ast.IsNot) and isinstance(node.test.comparators[0], ast.Constant) \
                and node.test.comparators[0].value is None:
            left = node.test.left
            if isinstance(left, ast.Name) and any(
                    isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "setattr"
                    for n in ast.walk(node)):
                return True
            if isinstance(left, ast.Attribute) and left.attr == field:
                return True
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and len(node.test.ops) == 1 \
                and isinstance(node.test.ops[0], ast.Is) and isinstance(node.test.comparators[0], ast.Constant) \
                and node.test.comparators[0].value is None and isinstance(node.test.left, ast.Attribute) \
                and node.test.left.attr == field and any(isinstance(n, ast.Raise) for n in node.body):
            return True
    return False


def nullable_into_not_null(modules=None) -> list[str]:
    """`模块:请求模型.字段→表.列`：请求体字段收得下 null，写进的却是不可空列，处理函数也没把 None 挡掉。

    写库形状与长度 / 数值两族共用（`test_body_str_length.body_column_writes`）。"""
    modules = list(bodystr._router_modules()) if modules is None else list(modules)
    # 映射表放在路由模块全部导入之后建：单独跑本文件时，模型是随路由模块一起注册进 Base 的
    orm = {m.class_.__name__: m.class_ for m in Base.registry.mappers}
    texts = {name: text for name, _, text in modules}
    out = set()
    for modname, cls, field, model_name, column in bodystr.body_column_writes(modules):
        info, model = cls.model_fields.get(field), orm.get(model_name)
        if info is None or model is None or not _nullable(info.annotation):
            continue
        col = model.__table__.columns.get(column)
        if col is None or col.nullable or col.primary_key:
            continue
        fns = [fn for fn in ast.parse(texts[modname]).body
               if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
               and any(a.annotation is not None and ast.unparse(a.annotation) == cls.__name__ for a in fn.args.args)]
        if any(not _none_skipped(fn, field) for fn in fns):
            short = modname.removeprefix("app.").removeprefix("routers.").replace("spd.routers.", "spd/")
            out.add(f"{short}:{cls.__name__}.{field}→{model.__tablename__}.{column}")
    return sorted(out)


def test_第二层_可空入参不得写进不可空列():
    offenders = nullable_into_not_null()
    assert len(offenders) <= NULLABLE_BASELINE, (
        "请求体字段收得下 null，写进的却是 NOT NULL 列——显式传 null 即 500。不可空的列声明成 `T`、"
        f"默认值取 `app/patchtypes.UNSET`（显式 null 是 422），或在处理函数里把 None 挡掉：{offenders}"
    )


def test_判据自证_第二层_照写的点名_挡掉的不报():
    from pydantic import Field

    snippet = '''
class LoosePatch(BaseModel):
    diagnosis_name: str | None = None

class GuardedPatch(BaseModel):
    diagnosis_name: str | None = None

class DroppedPatch(BaseModel):
    diagnosis_name: str | None = None

class StrictPatch(BaseModel):
    diagnosis_name: str = Field(default=None)

class AmendIn(BaseModel):
    summary: str | None = None

class AmendLooseIn(BaseModel):
    summary: str | None = None

class TransferIn(BaseModel):
    summary: str | None = None

@router.patch("/l/{i}")
def loose(i: int, body: LoosePatch, db=None):
    e = db.get(Encounter, i)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(e, k, v)

@router.patch("/g/{i}")
def guarded(i: int, body: GuardedPatch, db=None):
    e = db.get(Encounter, i)
    for k, v in body.model_dump(exclude_unset=True).items():
        if v is not None:
            setattr(e, k, v)

@router.patch("/d/{i}")
def dropped(i: int, body: DroppedPatch, db=None):
    e = db.get(Encounter, i)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(e, k, v)

@router.patch("/s/{i}")
def strict(i: int, body: StrictPatch, db=None):
    e = db.get(Encounter, i)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(e, k, v)

@router.post("/a/{i}")
def amend(i: int, body: AmendIn, db=None):
    e = db.get(Encounter, i)
    if body.summary is not None:
        e.summary = body.summary

@router.post("/b/{i}")
def amend_loose(i: int, body: AmendLooseIn, db=None):
    e = db.get(Encounter, i)
    e.summary = body.summary

@router.post("/t/{i}")
def transfer(i: int, body: TransferIn, db=None):
    e = db.get(Encounter, i)
    if body.summary is None:
        raise HTTPException(status_code=422, detail="必填")
    e.summary = body.summary
'''

    class _Router:
        def __getattr__(self, _name):
            return lambda *a, **k: (lambda fn: fn)

    mod = types.ModuleType("自证")
    mod.__dict__.update({"BaseModel": BaseModel, "Field": Field, "router": _Router()})
    exec(compile(snippet, "自证", "exec"), mod.__dict__)
    assert nullable_into_not_null([("自证", mod, snippet)]) == [
        "自证:AmendLooseIn.summary→encounters.summary",
        "自证:LoosePatch.diagnosis_name→encounters.diagnosis_name",
    ]


@pytest.mark.parametrize("path_fmt, field", [
    ("/programs/{program}", "name"),
    ("/programs/{program}", "active"),
    ("/followup-records/{record}", "status"),
    ("/followup-records/{record}", "planned_at"),
    ("/enrollments/{enrollment}", "stage"),
    ("/enrollments/{enrollment}", "next_followup_at"),
    ("/revisits/{revisit}", "plan_date"),
    ("/interventions/{intervention}", "status"),
    ("/path-instances/{instance}", "status"),
])
def test_第二层_不可空的列显式传null是422而不是500(client, admin, p195_world, path_fmt, field):
    resp = client.patch(B + path_fmt.format(**p195_world), headers=admin, json={field: None})
    assert resp.status_code == 422, (path_fmt, field, resp.text)


@pytest.fixture(scope="module")
def p195_world(client, admin):
    from app.database import SessionLocal
    from app.models import User
    from app.spd import models as S

    program = client.get(f"{B}/programs", headers=admin).json()[0]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "空值回归", "id_card": "110101199001016395", "gender": "男",
        "birth_date": "1990-01-01", "phone": "13800006395"}).json()["id"]
    template = client.post(f"{B}/path-templates", headers=admin, json={
        "program_id": program["id"], "code": "P195-T", "name": "空值回归路径"})
    assert template.status_code == 201, template.text
    with SessionLocal() as db:
        org_id = db.query(User).filter(User.username == "admin").one().org_id or 1
        enrollment = S.SpdEnrollment(patient_id=patient, program_code=program["code"], org_id=org_id,
                                     stage="", status="active")
        db.add(enrollment)
        db.flush()
        instance = S.SpdPathInstance(enrollment_id=enrollment.id, template_id=template.json()["id"])
        db.add(instance)
        db.commit()
        enrollment_id, instance_id = enrollment.id, instance.id
    rule = client.post(f"{B}/followup-rules", headers=admin,
                       json={"code": "P195-R", "name": "空值回归方案", "points": [7]}).json()
    plan = client.post(f"{B}/followup-plans", headers=admin, json={"patient_id": patient, "rule_id": rule["id"]})
    assert plan.status_code == 201, plan.text
    revisit = client.post(f"{B}/revisits", headers=admin, json={"patient_id": patient, "plan_date": "2026-10-10"})
    assert revisit.status_code == 201, revisit.text
    intervention = client.post(f"{B}/interventions", headers=admin, json={
        "patient_ids": [patient], "goal": "控压", "content": "低盐饮食", "program_code": program["code"]})
    assert intervention.status_code == 201, intervention.text
    return {"program": program["id"], "record": plan.json()["items"][0]["id"], "enrollment": enrollment_id,
            "revisit": revisit.json()["id"], "intervention": intervention.json()["ids"][0], "instance": instance_id}
