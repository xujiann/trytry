"""请求体字符串没有长度上限就写进 `String(N)` 列：生产库上超长即 500（P1-91）。

写接口把请求体的字符串字段写进 `String(N)` 列，入参却不带 `max_length`（也不是锚定的枚举
`pattern`、日期类校验器或 `Literal`）。于是：

- **开发库（SQLite）不管 VARCHAR 长度**，照存——开发、测试一律绿；
- **生产库（PostgreSQL）超长即抛 `StringDataRightTruncation`**，没人接，整个请求 **500**。

最容易撞的是自由文本：就诊摘要（`String(1024)`）、转诊理由、各种备注与理由（`String(256/512)`）——
医生把一段长一点的病情描述贴进文本框，整条就诊登记就没了，页面只剩「Internal Server Error」。

**本文件**：①棘轮——这种「入参无上限 → 定长列」的字段只减不增（判据见 `unbounded_body_strings`）；
②逐批修过的端点的回归：超长 422、恰好到上限照常收。修法一律是给请求模型字段补
`max_length=列长`（出参模型若继承了请求模型会一并带上这条约束——PG 上存量不可能超过列长，
所以对读侧是空操作，见 P2-40）。

**生产库那一半要在真 PG 上跑才算数**：`tests/test_postgres_real.py` 末尾有一条 integration 用例，
用 `MEDPLAT_STRLEN_PG_URL` 把本文件换到 PG 上再跑一遍（接法照抄 `test_date_filter_pg_dialect.py`）。
"""
import ast
import importlib
import inspect
import os
import pathlib
import re
import typing

# 引擎是模块级的，`app.database` 一旦导入就定型——切库必须赶在导入之前。
_PG_URL = os.environ.get("MEDPLAT_STRLEN_PG_URL", "")
if _PG_URL:
    os.environ["MEDPLAT_DATABASE_URL"] = _PG_URL

import pytest  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app.database import engine  # noqa: E402

if _PG_URL:
    assert engine.dialect.name == "postgresql", (
        "MEDPLAT_STRLEN_PG_URL 已给出，引擎却不是 PostgreSQL——"
        f"实际 {engine.dialect.name}，多半是 app.database 在本模块之前就被导入了"
    )

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

#: 基线：只许调小，已清零（`scripts/dump_gate_status.py` 把它列进闸门现状）。
#: 2026-09-24 实测 197 处（量法见 `unbounded_body_strings`）→ 182（第一批核心诊疗：就诊、入院、转诊、
#: 传染病报告、接种、满意度，15 个字段）→ 97（第二批诊疗与公卫：schemas 共用请求模型、孕产妇、临床文书、急救、
#: 处方、检查、医保、证明、慢病、上门、用血、手术、老年、短缺药、会诊、预约、公卫、质控，85 个字段）→ 0（第三批：
#: 管理侧 20 个文件与慢专病 3 个文件 90 个字段；最后一处是列本身太窄——角色变更留痕的两列 16 装不下
#: 32 位的自定义角色键，迁移 c3e4f5a6b7d9 扩到 32）。已清零，此后即零基线闸门。
#: 第二层（同日）：判据补上「请求体列表里的子项逐个写库」的形状（`for item in body.items:`），又量出 4 处——
#: 处方明细的药品编码 / 名称、批量号源模板的资源名 / 时段，真 PG 上超长即 500；同批补齐，仍为 0。
#: 第三层（同日）：再补「取出来的对象上显式赋值」`x.列 = body.字段`，又量出 10 处——检查报告修订的结论 / 所见、
#: 上门派单人与服务记录、医废交接人、整改措施 / 完成说明 / 验证意见、远程咨询回复与医师名；同批补齐，仍为 0。
BASELINE = 0

_FINITE_PATTERN = re.compile(r"\^[^*+{]*\$")


def _column_lengths() -> dict[str, dict[str, int]]:
    import app.models  # noqa: F401
    import app.spd.models  # noqa: F401
    from sqlalchemy import String

    from app.database import Base
    return {m.class_.__name__: {c.name: c.type.length for c in m.columns
                                if isinstance(c.type, String) and getattr(c.type, "length", None)}
            for m in Base.registry.mappers}


def _bounded(field, limit: int) -> bool:
    """这个入参字段自己就挡得住超长：max_length ≤ 列长，或锚定无界量词的 pattern、日期类校验器、Literal。"""
    ann = field.annotation
    variants = (ann, *typing.get_args(ann))
    metas = list(field.metadata) + [m for a in variants for m in getattr(a, "__metadata__", ())]
    if any(type(m).__name__ in ("BeforeValidator", "AfterValidator") for m in metas):
        return True
    if any(typing.get_origin(a) is typing.Literal for a in variants):
        return True
    pattern = next((m.pattern for m in metas if getattr(m, "pattern", None)), None)
    if pattern and _FINITE_PATTERN.fullmatch(pattern):
        return True
    limit_in = next((m.max_length for m in metas if getattr(m, "max_length", None)), None)
    return limit_in is not None and limit_in <= limit


def _is_str(field) -> bool:
    ann = field.annotation
    return ann is str or str in typing.get_args(ann)


def _router_modules():
    for base in (APP_DIR / "routers", APP_DIR / "spd" / "routers"):
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts or p.name == "__init__.py":
                continue
            name = ".".join(p.relative_to(APP_DIR.parent).with_suffix("").parts)
            yield name, importlib.import_module(name), p.read_text(encoding="utf-8")


def _list_element_model(cls, field_name: str):
    """请求模型里 `list[子模型]` 字段的子模型；不是这种字段返回 None。"""
    info = cls.model_fields.get(field_name)
    if info is None:
        return None
    for tp in (info.annotation, *typing.get_args(info.annotation)):
        if typing.get_origin(tp) in (list, tuple, set):
            for arg in typing.get_args(tp):
                if inspect.isclass(arg) and issubclass(arg, BaseModel):
                    return arg
    return None


def body_column_writes(modules=None) -> list[tuple]:
    """写接口把请求体的哪个字段写进了哪张表的哪一列：`[(模块, 请求模型, 字段, ORM 模型, 列)]`。

    写库形状五种：构造 `Model(**body.model_dump(...))`、构造里显式 `列=body.字段`、`x = db.get(Model, …)`
    之后 `setattr(x, …)` 的改档循环、**请求体里的列表字段逐项写库**——`for item in body.items:` 之后
    对 `item` 用前两种写法（P1-91 第二层：处方明细、批量号源曾因此整个漏在判据之外；setattr 那一种不认
    循环变量，改档循环写的是取出来的对象），以及**取出来的对象上显式赋值** `x.列 = body.字段`（第三层：
    检查报告修订、上门派单、远程咨询回复这类流转端点都是这么写的）。取对象只认 `db.get`——经 helper 或
    查询取出来的对象仍看不见。数值那一族（`test_body_numeric_capacity.py`）共用这一份。
    """
    lengths = _column_lengths()
    out = []
    for modname, mod, text in (modules if modules is not None else _router_modules()):
        tree = ast.parse(text)
        for fn in [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            if not any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                       and d.func.attr in ("post", "put", "patch") for d in fn.decorator_list):
                continue
            params = {}
            for a in fn.args.args:
                cls = getattr(mod, ast.unparse(a.annotation), None) if a.annotation is not None else None
                if inspect.isclass(cls) and issubclass(cls, BaseModel):
                    params[a.arg] = cls
            if not params:
                continue
            items = dict(params)    # 构造的两种写法还认循环变量：for item in body.items
            for node in ast.walk(fn):
                if isinstance(node, ast.For) and isinstance(node.target, ast.Name) \
                        and isinstance(node.iter, ast.Attribute) and isinstance(node.iter.value, ast.Name) \
                        and node.iter.value.id in params:
                    elem = _list_element_model(params[node.iter.value.id], node.iter.attr)
                    if elem is not None:
                        items[node.target.id] = elem
            fetched = {t.id: node.value.args[0].id for node in ast.walk(fn)
                       if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                       and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "get"
                       and node.value.args and isinstance(node.value.args[0], ast.Name)
                       for t in node.targets if isinstance(t, ast.Name)}
            writes = []   # (请求模型, 字段, ORM 模型[, 列])
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in lengths:
                    for kw in node.keywords:
                        v = kw.value
                        if kw.arg is None and isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) \
                                and v.func.attr == "model_dump" and isinstance(v.func.value, ast.Name) \
                                and v.func.value.id in items:
                            excluded = {c.value for k2 in v.keywords if k2.arg == "exclude"
                                        for c in ast.walk(k2.value) if isinstance(c, ast.Constant)}
                            cls = items[v.func.value.id]
                            writes += [(cls, f, node.func.id) for f in cls.model_fields if f not in excluded]
                        elif kw.arg and isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) \
                                and v.value.id in items and v.attr in items[v.value.id].model_fields:
                            writes.append((items[v.value.id], v.attr, node.func.id, kw.arg))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "setattr" \
                        and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in fetched:
                    writes += [(cls, f, fetched[node.args[0].id]) for cls in params.values() for f in cls.model_fields]
                # 第五种：取出来的对象上显式赋值 `report.conclusion = body.conclusion`（改档、流转里最常见）
                if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                        and isinstance(node.targets[0], ast.Attribute) and isinstance(node.targets[0].value, ast.Name) \
                        and node.targets[0].value.id in fetched and isinstance(node.value, ast.Attribute) \
                        and isinstance(node.value.value, ast.Name) and node.value.value.id in items \
                        and node.value.attr in items[node.value.value.id].model_fields:
                    writes.append((items[node.value.value.id], node.value.attr,
                                   fetched[node.targets[0].value.id], node.targets[0].attr))
            for w in writes:
                out.append((modname, w[0], w[1], w[2], w[3] if len(w) == 4 else w[1]))
    return out


def unbounded_body_strings(modules=None) -> list[str]:
    """`模块:请求模型.字段→ORM模型.列(N)`：写接口把这个字符串字段写进定长列，入参却挡不住超长。

    写库形状见 `body_column_writes`。同一个请求模型字段被几个端点写进同一列，只算一处（修一次就全好了）。
    """
    lengths = _column_lengths()
    found = set()
    for modname, cls, field_name, model, column in body_column_writes(modules):
        limit = lengths.get(model, {}).get(column)
        field = cls.model_fields[field_name]
        if limit is None or not _is_str(field) or _bounded(field, limit):
            continue
        found.add(f"{modname.removeprefix('app.')}:{cls.__name__}.{field_name}→{model}.{column}({limit})")
    return sorted(found)


def test_入参无上限写进定长列_只减不增():
    bad = unbounded_body_strings()
    assert len(bad) <= BASELINE, (
        f"入参挡不住超长、却写进定长列的字段 {len(bad)} 处，超过基线 {BASELINE}——新增的是：\n  "
        + "\n  ".join(bad)
        + "\n\n生产库（PG）上超长即 500。给请求模型字段补 `max_length=列长`（或锚定的枚举 pattern / 日期类型）。"
    )


def test_修完请把基线调小():
    """基线只许变小：实测比基线少了，说明修过一批却没调——调小它，别让欠账名义上还挂着。"""
    assert len(unbounded_body_strings()) >= BASELINE, (
        f"实测 {len(unbounded_body_strings())} 处，比基线 {BASELINE} 少——把 BASELINE 调小并写上是哪一批"
    )


def test_判据自证_五种写库形状都点名_挡得住的不报():
    import types

    from pydantic import Field

    snippet = '''
class NoteIn(BaseModel):
    patient_id: int
    note: str = ""
    code: str = Field(default="", max_length=64)
    kind: str = Field(default="a", pattern="^(a|b)$")

class NotePatch(BaseModel):
    note: str | None = None

class LineIn(BaseModel):
    diagnosis_name: str = ""

class BatchIn(BaseModel):
    lines: list[LineIn] = []

@router.post("/n")
def create(body: NoteIn, db=None):
    db.add(Encounter(**body.model_dump(exclude={"code", "kind"})))

@router.post("/m")
def create_m(body: NoteIn, db=None):
    db.add(Encounter(patient_id=body.patient_id, summary=body.note))

@router.patch("/n/{i}")
def patch(i: int, body: NotePatch, db=None):
    e = db.get(Encounter, i)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(e, k, v)

@router.post("/b")
def batch(body: BatchIn, db=None):
    for line in body.lines:
        db.add(Encounter(**line.model_dump()))

@router.post("/n/{i}/amend")
def amend(i: int, body: NotePatch, db=None):
    e = db.get(Encounter, i)
    e.diagnosis_name = body.note
'''

    class _Router:
        def __getattr__(self, _name):
            return lambda *a, **k: (lambda fn: fn)

    mod = types.ModuleType("自证")
    mod.__dict__.update({"BaseModel": BaseModel, "Field": Field, "router": _Router()})
    exec(compile(snippet, "自证", "exec"), mod.__dict__)
    got = unbounded_body_strings([("自证", mod, snippet)])
    # Encounter 上没有 note 列：只有显式 `summary=body.note` 那一处写进了定长列；
    # 请求体列表里的子项逐个写库（第四种）、取出来的对象上显式赋值（第五种）同样点名
    assert got == ["自证:LineIn.diagnosis_name→Encounter.diagnosis_name(256)",
                   "自证:NoteIn.note→Encounter.summary(1024)",
                   "自证:NotePatch.note→Encounter.diagnosis_name(256)"], got


# ================================================================ 第一批：核心诊疗
@pytest.fixture(scope="module")
def world(client, admin):
    county = client.post("/api/organizations", json={"name": "长度校验县医院", "org_type": "lead_hospital",
                                                     "level": "county"}, headers=admin).json()["id"]
    township = client.post("/api/organizations", json={"name": "长度校验卫生院", "org_type": "township",
                                                        "level": "township"}, headers=admin).json()["id"]
    patient = client.post("/api/patients", json={"name": "长度校验患者", "id_card": "330191198001010091",
                                                 "gender": "男", "birth_date": "1980-01-01"}, headers=admin).json()["id"]
    ward = client.post("/api/inpatient/wards", json={"org_id": county, "name": "长度校验病区"}, headers=admin).json()["id"]
    bed = client.post("/api/inpatient/beds", json={"ward_id": ward, "bed_no": "L01"}, headers=admin).json()["id"]
    return {"county": county, "township": township, "patient": patient, "ward": ward, "bed": bed}


def _cases(w):
    """(路径, 请求体（填 X 的那个字段待定）, 字段, 列长, 恰好到上限时是否也发一遍)"""
    return [
        ("/api/encounters", {"patient_id": w["patient"], "org_id": w["county"]}, "summary", 1024, True),
        ("/api/encounters", {"patient_id": w["patient"], "org_id": w["county"]}, "diagnosis_name", 256, True),
        ("/api/referrals", {"patient_id": w["patient"], "from_org_id": w["township"], "to_org_id": w["county"],
                            "direction": "up"}, "reason", 512, True),
        ("/api/inpatient/admissions", {"patient_id": w["patient"], "ward_id": w["ward"], "bed_id": w["bed"]},
         "diagnosis_name", 256, False),
        ("/api/infectious/cases", {"org_id": w["county"], "disease_code": "A09", "onset_date": "2031-01-01"},
         "disease_name", 128, True),
        ("/api/vaccination/records", {"patient_id": w["patient"], "vaccine_code": "V01", "org_id": w["county"]},
         "vaccine_name", 128, True),
        ("/api/surveys", {"target_type": "encounter", "target_id": 1, "patient_id": w["patient"], "score": 5},
         "comment", 512, True),
    ]


CASE_IDS = ["就诊摘要", "就诊诊断", "转诊理由", "入院诊断", "传染病名", "疫苗名", "满意度意见"]


@pytest.mark.parametrize("index", range(len(CASE_IDS)), ids=CASE_IDS)
def test_第一批_超长_422而不是生产库500(client, admin, world, index):
    path, body, field, limit, _ = _cases(world)[index]
    r = client.post(path, json={**body, field: "长" * (limit + 1)}, headers=admin)
    assert r.status_code == 422, (path, field, r.status_code, r.text[:200])
    assert field in r.text


@pytest.mark.parametrize("index", range(len(CASE_IDS)), ids=CASE_IDS)
def test_第一批_恰好到上限照常收(client, admin, world, index):
    path, body, field, limit, send = _cases(world)[index]
    if not send:
        pytest.skip("入院会占床，恰好到上限那一遍由就诊诊断同一列长代表")
    r = client.post(path, json={**body, field: "长" * limit}, headers=admin)
    assert r.status_code in (200, 201), (path, field, r.status_code, r.text[:200])



# ================================================================ 第二层：请求体列表里的子项逐个写库
# 判据原先只认「请求体参数本身」写库，`for item in body.items: Model(**item.model_dump())` 整个在它视野之外——
# 处方明细与批量号源模板四个字段因此漏过了三批（`body_column_writes` 的第四种形状）。
@pytest.mark.parametrize("field,limit", [("drug_code", 64), ("drug_name", 128)])
def test_第二层_处方明细超长_422而不是生产库500(client, admin, world, field, limit):
    item = {"drug_code": "P191RX", "drug_name": "长度校验药", "daily_dose": 1, "days": 1}
    body = {"patient_id": world["patient"], "org_id": world["county"]}
    r = client.post("/api/prescriptions", json={**body, "items": [{**item, field: "长" * (limit + 1)}]},
                    headers=admin)
    assert r.status_code == 422, (field, r.status_code, r.text[:200])
    assert field in r.text
    r = client.post("/api/prescriptions", json={**body, "items": [{**item, field: "长" * limit}]}, headers=admin)
    assert r.status_code == 201, (field, r.status_code, r.text[:200])


@pytest.mark.parametrize("field,limit", [("resource_name", 128), ("slot_time", 16)])
def test_第二层_批量号源模板超长_422而不是生产库500(client, admin, world, field, limit):
    template = {"resource_type": "outpatient", "resource_name": "长度校验门诊", "slot_time": "08:00"}
    body = {"org_id": world["township"], "date_from": "2031-03-03", "date_to": "2031-03-03"}
    r = client.post("/api/appointments/slots/batch",
                    json={**body, "templates": [{**template, field: "长" * (limit + 1)}]}, headers=admin)
    assert r.status_code == 422, (field, r.status_code, r.text[:200])
    assert field in r.text
    r = client.post("/api/appointments/slots/batch",
                    json={**body, "templates": [{**template, field: "长" * limit}]}, headers=admin)
    assert r.status_code == 201 and r.json()["created"] == 1, (field, r.status_code, r.text[:200])


# ================================================================ 第三层：取出来的对象上显式赋值
# `waste.handler_name = body.handler_name` 这种流转端点的写法，判据原先只认 setattr 循环——检查报告修订、
# 上门派单与完成、医废交接、整改进度与验证、远程咨询回复共 10 个字段整个在视野之外（`body_column_writes`
# 的第五种形状）。
def test_第三层_医废交接人超长_422而不是生产库500(client, admin, world):
    loc = client.post("/api/medwaste/locations", json={"org_id": world["county"], "name": "长度校验产生点",
                                                       "location_type": "source"}, headers=admin)
    assert loc.status_code == 201, loc.text
    waste = client.post("/api/medwaste", json={"org_id": world["county"], "waste_type": "infectious", "weight_kg": 1,
                                               "collected_date": "2031-05-05", "source_location_id": loc.json()["id"]},
                        headers=admin)
    assert waste.status_code == 201, waste.text
    wid = waste.json()["id"]
    r = client.post(f"/api/medwaste/{wid}/handover", json={"handler_name": "长" * 65}, headers=admin)
    assert r.status_code == 422 and "handler_name" in r.text, (r.status_code, r.text[:200])
    r = client.post(f"/api/medwaste/{wid}/handover", json={"handler_name": "长" * 64}, headers=admin)
    assert r.status_code == 200, (r.status_code, r.text[:200])


def test_改成长键自定义角色_留痕列装得下(client, admin):
    """角色变更留痕的两列原先只有 16，自定义角色键最长 32：PG 上改成 / 改离长键角色即 500（迁移 c3e4f5a6b7d9）。"""
    key = "p191_custom_role_ab"   # 19 位，合法的自定义角色键
    r = client.post("/api/rbac/roles", json={"key": key, "name": "长键自定义角色"}, headers=admin)
    assert r.status_code == 201, r.text
    r = client.post("/api/users", json={"username": "p191_roleuser", "password": "passw0rd1",
                                        "full_name": "长键角色用户", "role": "operator"}, headers=admin)
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    r = client.patch(f"/api/users/{uid}/role", json={"role": key}, headers=admin)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    r = client.patch(f"/api/users/{uid}/role", json={"role": "operator"}, headers=admin)
    assert r.status_code == 200, (r.status_code, r.text[:200])
