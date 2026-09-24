"""请求体浮点字段收下 NaN / Infinity：生产库照存，超标判定对 NaN 恒为假（P1-92）。

pydantic 的 `float` 默认放行非有限值，而且两条路都进得来：

- JSON 里的 `NaN` / `Infinity` / `-Infinity` 记号（Python 标准库 `json.dumps` 默认就这么写）与 `1e400`
  这种溢出的字面量——FastAPI 用标准库 `json.loads` 解析请求体，照单全收；
- **字符串** `"NaN"` / `"Infinity"`：pydantic 宽松模式把它转成 float（Java 生态的 Jackson 默认就把
  `Double.NaN` 写成字符串 `"NaN"`）。界面发不出这两种值，走得到这里的是对接方与脚本——
  而探头坏了、仪器没读出数，恰恰就是这类调用方手里出现 NaN 的时候。

收下之后（2026-09-24 实测，真 PG）：

- **判定悄悄失效**：`NaN < 下限 or NaN > 上限` 恒为假。冷链录温 `temperature=NaN` → 201、`exceeded=false`
  ——坏掉的温度探头被记成「未超温」；质控测定 `value=NaN` → 201、`out_of_control=false`——Westgard 判定
  整个失效。同样的比较还挂在随访血压/血糖、慢专病测量值、公卫监测值、急救生命体征上。
- **金额列 500**：`Numeric(14,2)` 装不下无穷，`price=Infinity` / `1e400` → 500（SQLite 照存，读出来是 null）。
- **连 422 都回不出来**：带约束的字段（`Field(gt=0)`）收到 `NaN` 记号，校验是拦下了，可错误详情里的
  `input` 是个 NaN，而 FastAPI 的 422 用 `json.dumps(allow_nan=False)` 编码——编码失败，**422 变 500**
  （两个库都一样）。

修法：

1. `main.py` 接管 422：错误详情里的非有限值换成它在 JSON 里本来的写法（字符串），其余逐字节照旧；
2. 请求体里的浮点一律 `FiniteFloat`（上下界都有的本来就挡得住，不动）；出参模型若继承了请求模型，
   把这些字段覆盖回 `float`——PG 的 float / numeric 列**存得下** NaN，出参再校验有限性，就是 P1-63 那种
   「一条存量坏值让整个列表 500」；存量坏值该原样读出（null），看得见才改得了；
3. 浮点查询参数同样拒收（押金预警的阈值）。

**生产库那一半要在真 PG 上跑才算数**：`tests/test_postgres_real.py` 末尾有一条 integration 用例，用
`MEDPLAT_FINITE_PG_URL` 把本文件换到 PG 上再跑一遍——PG 上存量坏值还能是 NaN（SQLite 把 NaN 存成 NULL）。
"""
import json
import math
import os
import types
import typing

# 引擎是模块级的，`app.database` 一旦导入就定型——切库必须赶在导入之前。
_PG_URL = os.environ.get("MEDPLAT_FINITE_PG_URL", "")
if _PG_URL:
    os.environ["MEDPLAT_DATABASE_URL"] = _PG_URL

import pytest  # noqa: E402
import test_api_contract_governance as contract  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app.database import engine  # noqa: E402

if _PG_URL:
    assert engine.dialect.name == "postgresql", (
        "MEDPLAT_FINITE_PG_URL 已给出，引擎却不是 PostgreSQL——"
        f"实际 {engine.dialect.name}，多半是 app.database 在本模块之前就被导入了"
    )

#: 基线：已清零，此后即零基线闸门（`scripts/dump_gate_status.py` 把它列进闸门现状）。
#: 2026-09-24 实测 66 处：请求体浮点字段 65 个（22 个一个界都没有、43 个只有下界——`gt=0` 挡得住 NaN
#: 却放行 +Infinity），浮点查询参数 1 个（押金预警阈值）；同一批修完。
BASELINE = 0

RAW_JSON = {"content-type": "application/json"}
_UNION = (typing.Union, types.UnionType)


# ================================================================ 判据
def _finite_guard(metas) -> bool:
    """这些约束挡得住非有限值：显式 `allow_inf_nan=False`（`FiniteFloat` / `Field(allow_inf_nan=False)`），
    或上下界都有（NaN 过不了比较，±Infinity 总会越过一头）。"""
    if any(getattr(m, "allow_inf_nan", None) is False for m in metas):
        return True
    has = lambda *keys: any(getattr(m, k, None) is not None for m in metas for k in keys)  # noqa: E731
    return has("ge", "gt") and has("le", "lt")


def _unguarded_float(tp, metas=()) -> bool:
    """`tp` 里有没有挡不住非有限值的 float。字段级约束只顺着可空（Union）往下传，不进容器——
    `dict[str, float] = Field(...)` 的约束管的是 dict 本身，不是里面的值。"""
    if typing.get_origin(tp) is typing.Annotated:
        base, *inner = typing.get_args(tp)
        return _unguarded_float(base, (*metas, *inner))
    if tp is float:
        return not _finite_guard(metas)
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        return False    # 嵌套的请求模型自己也在被查的集合里
    passes = metas if typing.get_origin(tp) in _UNION else ()
    return any(_unguarded_float(a, passes) for a in typing.get_args(tp))


def _models_in(tp, seen: set) -> None:
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        if tp not in seen:
            seen.add(tp)
            for info in tp.model_fields.values():
                _models_in(info.annotation, seen)
        return
    for arg in typing.get_args(tp):
        _models_in(arg, seen)


def _dependants(dep):
    yield dep
    for sub in dep.dependencies:
        yield from _dependants(sub)


def request_models() -> set:
    """出现在路由请求体里的模型，顺着嵌套一路找全。"""
    seen: set = set()
    for _, route in contract._iter_endpoints():
        for dep in _dependants(route.dependant):
            for param in dep.body_params:
                _models_in(param.field_info.annotation, seen)
    return seen


def nonfinite_body_floats(models=None) -> list[str]:
    """`模块:请求模型.字段`：请求体里挡不住 NaN / Infinity 的浮点字段。"""
    return sorted(
        f"{m.__module__.removeprefix('app.')}:{m.__qualname__}.{name}"
        for m in (request_models() if models is None else models)
        for name, info in m.model_fields.items()
        if _unguarded_float(info.annotation, tuple(info.metadata))
    )


def nonfinite_query_floats() -> list[str]:
    return sorted({
        f"{'/'.join(sorted(route.methods))} {route.path} ?{q.name}"
        for _, route in contract._iter_endpoints()
        for dep in _dependants(route.dependant)
        for q in dep.query_params
        if _unguarded_float(q.field_info.annotation, tuple(q.field_info.metadata))
    })


def _finite_only(tp, seen: set, metas=()) -> list[str]:
    """`tp` 里（顺着 list/dict/Optional/嵌套模型）要求有限值的浮点：[模块:模型.字段]。

    直接落在 float 上的命中先返回空串，由外层的模型补上是哪个字段。"""
    if typing.get_origin(tp) is typing.Annotated:
        base, *inner = typing.get_args(tp)
        return _finite_only(base, seen, (*metas, *inner))
    if tp is float:
        return [""] if any(getattr(m, "allow_inf_nan", None) is False for m in metas) else []
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        if tp in seen:
            return []
        seen.add(tp)
        return [hit or f"{tp.__module__}:{tp.__qualname__}.{name}"
                for name, info in tp.model_fields.items()
                for hit in _finite_only(info.annotation, seen, tuple(info.metadata))]
    passes = metas if typing.get_origin(tp) in _UNION else ()
    return [hit for arg in typing.get_args(tp) for hit in _finite_only(arg, seen, passes)]


def finite_only_response_fields() -> list[str]:
    return sorted({
        f"{hit} ← {'/'.join(sorted(route.methods))} {route.path}"
        for _, route in contract._iter_endpoints()
        if route.response_model is not None
        for hit in _finite_only(route.response_model, set()) if hit
    })


def test_请求体浮点字段一律挡住NaN与Infinity():
    bad = nonfinite_body_floats()
    assert len(bad) <= BASELINE, (
        f"请求体里 {len(bad)} 个浮点字段收得下 NaN / Infinity（含字符串 \"NaN\"）——生产库照存、"
        "超标判定对 NaN 恒为假、金额列上 500。改成 `FiniteFloat`（`from pydantic import FiniteFloat`）"
        "或给上下界；出参若继承了它，记得覆盖回 `float`：\n  " + "\n  ".join(bad)
    )


def test_浮点查询参数一律挡住NaN与Infinity():
    bad = nonfinite_query_floats()
    assert len(bad) <= BASELINE, (
        "这些浮点查询参数收得下 `nan` / `inf`——拿它去比较，整张清单悄悄变空或变全："
        "`Query(..., allow_inf_nan=False)` 或给上下界：\n  " + "\n  ".join(bad)
    )


def test_出参模型不要求有限值():
    """PG 的 float / numeric 列存得下 NaN：出参再校验有限性，一条存量坏值就让整个列表 500（P1-63 同形状）。"""
    bad = finite_only_response_fields()
    assert not bad, (
        "这些响应模型带着 `allow_inf_nan=False`（多半是继承了请求模型）——库里一条存量 NaN 就让整个"
        "响应 500。在出参模型里把字段覆盖回 `float`（字段顺序不变，响应字节不变）：\n  " + "\n  ".join(bad)
    )


def test_判据自证_裸浮点_单边界_容器里的浮点都认得出_挡得住的不报():
    from pydantic import Field, FiniteFloat

    class _In(BaseModel):
        bare: float
        low_only: float = Field(gt=0)
        optional: float | None = None
        values: dict[str, float] = Field(default_factory=dict)
        finite: FiniteFloat
        finite_low: FiniteFloat | None = Field(default=None, gt=0)
        both: float = Field(ge=0, le=100)
        explicit: float = Field(default=0, allow_inf_nan=False)
        values_ok: dict[str, FiniteFloat] = Field(default_factory=dict)
        count: int = 0

    class _Out(_In):   # 本条要抓的出参形状：继承了请求模型
        id: int

    class _Fixed(_In):
        id: int
        finite: float
        finite_low: float | None = None
        explicit: float = 0
        values_ok: dict[str, float] = Field(default_factory=dict)

    got = [x.split(".")[-1] for x in nonfinite_body_floats({_In})]
    assert got == ["bare", "low_only", "optional", "values"], got
    got = sorted(x.split(".")[-1] for x in _finite_only(_Out, set()) if x)
    assert got == ["explicit", "finite", "finite_low", "values_ok"], got
    assert [x for x in _finite_only(_Fixed, set()) if x] == []
    assert list(_Fixed.model_fields) == [*_In.model_fields, "id"], "覆盖不该挪动字段顺序（响应字节不变）"


# ================================================================ 422 兜底
def test_422兜底_NaN记号进带约束字段是422不是500(client, admin):
    r = client.post("/api/billing/charge-items", content=b'{"code":"P192A","name":"x","price":NaN}',
                    headers={**admin, **RAW_JSON})
    assert r.status_code == 422, (r.status_code, r.text[:200])
    detail = r.json()["detail"]
    assert detail[0]["loc"] == ["body", "price"] and detail[0]["input"] == "NaN", detail


def test_422兜底_整个请求体当input时里面的NaN也编得出来(client, admin):
    """缺必填字段的错误把**整个请求体**放进 input——别的字段里的 NaN 一样会让编码失败。"""
    r = client.post("/api/billing/charge-items", content=b'{"name":"x","price":-Infinity,"extra":[NaN]}',
                    headers={**admin, **RAW_JSON})
    assert r.status_code == 422, (r.status_code, r.text[:200])
    text = r.text
    assert '"-Infinity"' in text and '["NaN"]' in text, text[:400]


def test_422兜底_有限值的错误详情与FastAPI默认逐字节一致():
    """特征化：接管 422 只为兜住非有限值，其余响应一个字节都不许变（CLAUDE.md §11）。"""
    import asyncio

    from fastapi.exception_handlers import request_validation_exception_handler
    from fastapi.exceptions import RequestValidationError
    from pydantic import Field, ValidationError, field_validator

    from app.main import app

    class _Probe(BaseModel):
        price: float = Field(gt=0)
        code: str = Field(max_length=2)
        day: str

        @field_validator("day")
        @classmethod
        def _day(cls, v):
            raise ValueError("日期不对")

    try:
        _Probe.model_validate({"price": -1.5, "code": "长长长", "day": "x", "n": [1, 2.5]})
    except ValidationError as e:
        exc = RequestValidationError(e.errors())
    ours = app.exception_handlers[RequestValidationError]
    expected = asyncio.run(request_validation_exception_handler(None, exc))
    got = asyncio.run(ours(None, exc))
    assert got.status_code == expected.status_code == 422
    assert got.body == expected.body, (got.body, expected.body)
    assert dict(got.headers) == dict(expected.headers)


# ================================================================ 业务回归
@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", json={"name": "有限值卫生院", "org_type": "township",
                                                  "level": "township"}, headers=admin).json()["id"]
    r = client.post("/api/labqc/lots", json={"org_id": org, "item_code": "GLU", "item_name": "葡萄糖",
                                             "lot_no": "P192", "target_value": 5.0, "sd": 0.2}, headers=admin)
    assert r.status_code == 201, r.text
    return {"org": org, "lot": r.json()["id"]}


def _cold_chain(client, admin, world, temperature: str, **extra):
    body = {"org_id": world["org"], "device_name": "有限值冰箱", "recorded_at": "2031-01-01 08:00", **extra}
    text = json.dumps(body, ensure_ascii=False)[:-1] + f',"temperature":{temperature}}}'
    return client.post("/api/vaccine-supply/cold-chain", content=text.encode(), headers={**admin, **RAW_JSON})


@pytest.mark.parametrize("temperature", ["NaN", '"NaN"', "Infinity", '"-Infinity"', "1e400"])
def test_冷链录温_非有限温度422_不入库(client, admin, world, temperature):
    before = client.get(f"/api/vaccine-supply/cold-chain?org_id={world['org']}", headers=admin).json()
    r = _cold_chain(client, admin, world, temperature)
    assert r.status_code == 422, (temperature, r.status_code, r.text[:200])
    after = client.get(f"/api/vaccine-supply/cold-chain?org_id={world['org']}", headers=admin).json()
    assert after == before


def test_冷链录温_允许区间给NaN也422(client, admin, world):
    r = _cold_chain(client, admin, world, "9.5", min_allowed=float("nan"))
    assert r.status_code == 422, (r.status_code, r.text[:200])


def test_特征化_冷链录温有限值照常判超温(client, admin, world):
    r = _cold_chain(client, admin, world, "9.5")
    assert r.status_code == 201 and r.json()["exceeded"] is True, r.text
    r = _cold_chain(client, admin, world, "4.0")
    assert r.status_code == 201 and r.json()["exceeded"] is False, r.text


@pytest.mark.parametrize("value", ["NaN", '"NaN"', "Infinity"])
def test_质控测定_非有限测定值422(client, admin, world, value):
    r = client.post(f"/api/labqc/lots/{world['lot']}/measurements", content=f'{{"value":{value}}}'.encode(),
                    headers={**admin, **RAW_JSON})
    assert r.status_code == 422, (value, r.status_code, r.text[:200])


def test_特征化_质控测定有限值照常判失控(client, admin, world):
    r = client.post(f"/api/labqc/lots/{world['lot']}/measurements", json={"value": 5.0}, headers=admin)
    assert r.status_code == 201 and r.json()["out_of_control"] is False, r.text
    r = client.post(f"/api/labqc/lots/{world['lot']}/measurements", json={"value": 6.0}, headers=admin)
    assert r.status_code == 201 and r.json()["out_of_control"] is True, r.text   # z = 5，超 1-3s


@pytest.mark.parametrize("price", ["Infinity", "1e400", '"Infinity"'])
def test_收费项目_无穷单价422而不是生产库500(client, admin, price):
    r = client.post("/api/billing/charge-items", content=f'{{"code":"P192P","name":"x","price":{price}}}'.encode(),
                    headers={**admin, **RAW_JSON})
    assert r.status_code == 422, (price, r.status_code, r.text[:200])


def test_押金预警_阈值nan是422(client, admin):
    for value in ("nan", "inf"):
        r = client.get(f"/api/billing/deposits/alerts?threshold={value}", headers=admin)
        assert r.status_code == 422, (value, r.status_code, r.text[:200])
    assert client.get("/api/billing/deposits/alerts?threshold=500", headers=admin).status_code == 200


# ================================================================ 存量坏值照常读出
def _stored_bad_values():
    """SQLite 把 NaN 存成 NULL（非空列直接拒），能存进去的只有 ±Infinity；PG 两样都存得下。"""
    return [math.inf, math.nan] if engine.dialect.name == "postgresql" else [math.inf]


@pytest.mark.parametrize("bad", _stored_bad_values(), ids=repr)
def test_存量非有限值_读接口照常200_坏值读成null(client, admin, world, bad):
    from app.database import SessionLocal
    from app.models import ColdChainRecord, QcLot

    db = SessionLocal()
    try:
        db.add(ColdChainRecord(org_id=world["org"], device_name="存量坏值冰箱", temperature=bad,
                               min_allowed=2.0, max_allowed=8.0, exceeded=False, recorded_at="2031-01-02 08:00"))
        lot = QcLot(org_id=world["org"], item_code="BAD", item_name="存量坏值", lot_no=f"BAD{bad}",
                    target_value=bad, sd=0.2)
        db.add(lot)
        db.commit()
        lot_id = lot.id
    finally:
        db.close()
    r = client.get(f"/api/vaccine-supply/cold-chain?org_id={world['org']}", headers=admin)
    assert r.status_code == 200, r.text[:200]
    assert any(row["device_name"] == "存量坏值冰箱" and row["temperature"] is None for row in r.json())
    r = client.get(f"/api/labqc/lots?org_id={world['org']}", headers=admin)
    assert r.status_code == 200, r.text[:200]
    assert any(row["id"] == lot_id and row["target_value"] is None for row in r.json()), r.json()
