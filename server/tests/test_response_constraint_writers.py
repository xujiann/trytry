"""出参字段带的约束，写同一列的每个入口都得至少一样严（P2-40）。

`class XxxOut(XxxCreate)` 把入参的 `pattern` / `min_length` / `max_length` / 数值上下界一起带到出参上；
FastAPI 拿 `response_model` 校验出参，库里只要有一行不满足，**整个响应 500**（P1-63 日期、P1-65 证件号
是同族已修的两例）。建档入口挡得住，存量违例就只可能来自：①写同一列的别的入口比建档松；②绕过入口的
写入（导入脚本、对接入站、直改库）；③建档后被业务逻辑改写。

2026-09-24 逐字段审完 71 个带约束的出参字段（枚举 `pattern` 21 个、数值上下界与 `min_length` 50 个；
`max_length` 此前已机械筛过，见 TECH_DEBT P2-40）：

- ① 1 处：互认目录改档 `RecognitionItemUpdate.item_name` 不带 `min_length`——`PATCH` 改名为空串照收，
  commit 之后出参校验失败，那一次 500；此后 `GET /api/exams/recognition-items` **整张互认目录 500**，
  互认目录页整页打不开，只能直改库恢复（修前实测）。
- ② 1 处：存量导入 `scripts/import_legacy.py` 的机构名只查非空，建档要 2～128 字——导进一个单字机构名，
  `GET /api/organizations` **对所有人 500**（各页的机构下拉都靠它；修前实测）。
- ③ 未见：数量类的扣减（物资出库、药品调拨、库存扣减）全走带条件的原子更新，增量都是正数；
  列默认值、迁移的 `server_default`、种子与导入脚本写进这些列的常量都在约束之内。

修法两半：写入方收紧到与建档同口径（改档补 `min_length=1`，导入按 `OrganizationCreate` 校验）；
这两个字段的出参再按 P1-63 覆盖回 `str`——修之前已经存进去的坏值要在清单里**看得见**才谈得上改，
出参的职责是把库里有什么原样交出来。

守卫（派生、零基线、无豁免名单）：所有路由的 `response_model` 顺着嵌套模型查下去，带约束的出参字段按
`test_body_str_length.body_column_writes` 对到它写的列（出参从哪个请求模型继承，就认那个请求模型写的列），
写同一列的**每个**请求模型字段必须至少一样严。看不见的写入方（上面的②③）不在守卫之内，靠本文件的
回归与 P2-40 的逐字段记录。
"""
from __future__ import annotations

import re
import typing

import pytest
import test_api_contract_governance as contract
import test_body_str_length as strlen
from pydantic import BaseModel, Field

from app.database import SessionLocal
from app.models import Organization, RecognitionItem

#: 基线：已清零，此后即零基线闸门（`scripts/dump_gate_status.py` 把它列进闸门现状）。
#: 2026-09-24 实测 1 处（互认目录改档的 `item_name`），同一批修完。
BASELINE = 0

_KINDS = ("pattern", "min_length", "max_length", "ge", "gt", "le", "lt")


def _unwrap(tp):
    return typing.get_args(tp)[0] if typing.get_origin(tp) is typing.Annotated else tp


def _effective(info) -> dict:
    """字段实际挡得住什么：`Field(...)` 的约束，加上 `X | None` 里 Annotated 成员带的（`FiniteFloat | None` 一类）。

    整数的开区间折成闭区间（`gt=0` 与 `ge=1` 同义）；下界 / 上界记成 `(值, 是否开区间)`。
    """
    ann = info.annotation
    members = (ann, *typing.get_args(ann))
    metas = list(info.metadata) + [m for a in members for m in getattr(a, "__metadata__", ())]

    def vals(kind):
        return [getattr(m, kind) for m in metas if getattr(m, kind, None) is not None]

    is_int = int in {_unwrap(a) for a in members}
    lows = [(v, False) for v in vals("ge")] + [(v, True) for v in vals("gt")]
    highs = [(v, False) for v in vals("le")] + [(v, True) for v in vals("lt")]
    if is_int:
        lows = [(v + 1, False) if strict else (v, False) for v, strict in lows]
        highs = [(v - 1, False) if strict else (v, False) for v, strict in highs]
    literals = [v for a in members if typing.get_origin(_unwrap(a)) is typing.Literal
                for v in typing.get_args(_unwrap(a))]
    return {
        "pattern": set(vals("pattern")),
        "literals": literals,
        "min_length": max(vals("min_length"), default=0),
        "max_length": min(vals("max_length"), default=None),
        "low": max(lows, default=None),
        # 上界越小越严；同值时开区间更严
        "high": min(highs, key=lambda h: (h[0], not h[1]), default=None),
    }


def _looser(out_info, writer_info) -> list[str]:
    """写入方比出参松在哪几项；一样严或更严返回空。"""
    out, w = _effective(out_info), _effective(writer_info)
    found = []
    if out["pattern"] and not (out["pattern"] <= w["pattern"] or (
            w["literals"] and all(isinstance(v, str) and re.fullmatch(p, v)
                                  for v in w["literals"] for p in out["pattern"]))):
        found.append("pattern")
    if w["min_length"] < out["min_length"]:
        found.append("min_length")
    if out["max_length"] is not None and (w["max_length"] is None or w["max_length"] > out["max_length"]):
        found.append("max_length")
    if out["low"] is not None and (w["low"] is None or w["low"] < out["low"]):
        found.append("下界")
    if out["high"] is not None and (w["high"] is None or (w["high"][0], not w["high"][1])
                                    > (out["high"][0], not out["high"][1])):
        found.append("上界")
    return found


def _models_in(tp, seen: set):
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        if tp not in seen:
            seen.add(tp)
            yield tp
            for info in tp.model_fields.values():
                yield from _models_in(info.annotation, seen)
        return
    for arg in typing.get_args(tp):
        yield from _models_in(arg, seen)


def constrained_output_writers() -> dict[tuple[type, str], list[tuple[str, type, str]]]:
    """带约束的出参字段 → 写同一列的全部请求模型字段 `[(模块, 请求模型, 字段)]`（对不上列的不收）。"""
    outputs: set[tuple[type, str]] = set()
    seen: set = set()
    for _name, route in contract._iter_endpoints():
        if route.response_model is None:
            continue
        for cls in _models_in(route.response_model, seen):
            outputs |= {(cls, f) for f, info in cls.model_fields.items()
                        if any(getattr(m, k, None) is not None for m in info.metadata for k in _KINDS)}
    writes = strlen.body_column_writes()
    by_column: dict[tuple[str, str], set] = {}
    for modname, req, field, model, column in writes:
        by_column.setdefault((model, column), set()).add((modname, req, field))
    mapped = {}
    for cls, field in outputs:
        columns = {(model, column) for _m, req, f, model, column in writes if f == field and issubclass(cls, req)}
        writers = sorted({w for c in columns for w in by_column[c]}, key=lambda w: (w[0], w[1].__name__, w[2]))
        if writers:
            mapped[(cls, field)] = writers
    return mapped


def looser_writers() -> list[str]:
    """`出参模型.字段 ← 模块:请求模型.字段 松在 [...]`：写同一列的入口比出参松，存得出让整个响应 500 的行。"""
    found = []
    for (cls, field), writers in constrained_output_writers().items():
        for modname, req, f in writers:
            kinds = _looser(cls.model_fields[field], req.model_fields[f])
            if kinds:
                found.append(f"{cls.__name__}.{field} ← {modname.removeprefix('app.')}:{req.__name__}.{f} 松在 {kinds}")
    return sorted(found)


# ================================================================ 守卫
def test_写同一列的入口不比出参松():
    offenders = looser_writers()
    assert len(offenders) <= BASELINE, (
        "写同一列的请求模型比出参约束松：存得进去、读不出来，库里一行就让整个响应 500。"
        f"把写入方收紧到与建档同口径（改档模型尤其容易漏 min_length / pattern）：{offenders}"
    )


def test_守卫看得见写入方_号源出参对上建档与批量模板两个入口():
    """防判据悄悄失明（扫描口径变了、一个字段都对不上时，上面那条会空转成绿）：号源出参的容量与资源类型
    必须对上单建号源 `SlotCreate` 与批量模板 `SlotTemplate` 两个写入方。"""
    mapped = {(cls.__name__, field): {f"{req.__name__}.{f}" for _m, req, f in writers}
              for (cls, field), writers in constrained_output_writers().items()}
    for field in ("capacity", "resource_type", "resource_name"):
        assert mapped.get(("SlotOut", field)) == {f"SlotCreate.{field}", f"SlotTemplate.{field}"}, mapped.get(
            ("SlotOut", field))


# ================================================================ 守卫：出参带校验器（上面那道看不见的一半）
#
# 上面的守卫只看声明式约束（pattern / 长度 / 数值界）。`class XxxOut(XxxCreate)` 同样会把请求模型的
# `@field_validator` / `@model_validator` 带到出参上——库里一行过不了，整个响应照样 500。2026-09-24 补查：
# 全部 response_model（顺嵌套）里带校验器的只有 1 个，写明为什么读得出来；新增即红，名单只减不增。
# （`datetypes` 那类 Annotated 校验器不在这里，由 `test_response_date_types.py` 盯着。）

#: 带校验器的出参模型（`模块:类名`）→ 为什么读得出来
OUTPUT_VALIDATORS_OK = {
    "app.routers.emergency:MilestoneOut":
        "只作建节点那一次的回执：回显的就是刚过同一道 ISO 校验的值；时间轴读取走 TimelineNodeOut（str | None）",
}


def _has_validators(cls: type) -> bool:
    decorators = cls.__pydantic_decorators__
    return bool(decorators.field_validators or decorators.model_validators)


def output_models_with_validators() -> set[str]:
    seen: set = set()
    found = set()
    for _name, route in contract._iter_endpoints():
        if route.response_model is None:
            continue
        found |= {f"{cls.__module__}:{cls.__qualname__}"
                  for cls in _models_in(route.response_model, seen) if _has_validators(cls)}
    return found


def test_出参模型带校验器_须写明为什么读得出来():
    got = output_models_with_validators()
    new = sorted(got - OUTPUT_VALIDATORS_OK.keys())
    assert not new, (
        f"这些出参模型带着校验器（多半是从请求模型继承来的）：{new}\n库里一行过不了校验，整个响应 500。"
        "出参覆盖掉继承来的字段（P1-63 的写法），或写进 OUTPUT_VALIDATORS_OK 说明为什么读得出来。"
    )
    stale = sorted(OUTPUT_VALIDATORS_OK.keys() - got)
    assert not stale, f"这些已经不带校验器（或不再是出参）了，请从 OUTPUT_VALIDATORS_OK 划掉：{stale}"


def test_判据自证_继承来的校验器也认得出():
    from pydantic import field_validator

    class In(BaseModel):
        at: str

        @field_validator("at")
        @classmethod
        def _iso(cls, value: str) -> str:
            return value

    class Out(In):
        id: int

    class Plain(BaseModel):
        at: str

    assert _has_validators(Out) and not _has_validators(Plain)


def test_判据自证_松的点名_一样严或更严的不报():
    class Out(BaseModel):
        name: str = Field(min_length=1, max_length=64)
        kind: str = Field(pattern="^(a|b)$")
        qty: int = Field(ge=0, le=100)

    class Same(BaseModel):
        name: str = Field(min_length=1, max_length=64)
        kind: typing.Literal["a", "b"]
        qty: int = Field(gt=-1, lt=101)

    class Stricter(BaseModel):
        name: str | None = Field(default=None, min_length=2, max_length=32)
        kind: str | None = Field(default=None, pattern="^(a|b)$")
        qty: int = Field(ge=1, le=99)

    class Looser(BaseModel):
        name: str | None = Field(default=None, max_length=64)
        kind: str = Field(max_length=8)
        qty: int = Field(ge=-1)

    for writer in (Same, Stricter):
        assert {f: _looser(Out.model_fields[f], writer.model_fields[f]) for f in Out.model_fields} == {
            "name": [], "kind": [], "qty": []}, writer
    assert {f: _looser(Out.model_fields[f], Looser.model_fields[f]) for f in Out.model_fields} == {
        "name": ["min_length"], "kind": ["pattern"], "qty": ["下界", "上界"]}


# ================================================================ 行为回归（修前实测见 docstring）
@pytest.fixture(scope="module")
def item(client, admin):
    resp = client.post("/api/exams/recognition-items", headers=admin, json={
        "item_code": "P240-CT", "item_name": "胸部CT平扫", "center_type": "imaging"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_互认目录改名为空串是422_目录照常可读(client, admin, item):
    bad = client.patch(f"/api/exams/recognition-items/{item}", headers=admin, json={"item_name": ""})
    assert bad.status_code == 422, bad.text
    listing = client.get("/api/exams/recognition-items", headers=admin)
    assert listing.status_code == 200, listing.text
    assert [r["item_name"] for r in listing.json() if r["id"] == item] == ["胸部CT平扫"]
    ok = client.patch(f"/api/exams/recognition-items/{item}", headers=admin, json={"item_name": "胸部CT增强"})
    assert ok.status_code == 200 and ok.json()["item_name"] == "胸部CT增强", ok.text


def test_存量坏值照常读出_不再拖垮整张清单(client, admin):
    """修之前已经存进去的空名目录项、导入进来的单字机构名：清单 200，坏值原样读出，才谈得上改。"""
    with SessionLocal() as db:
        db.add(RecognitionItem(item_code="P240-EMPTY", item_name="", center_type="lab"))
        db.add(Organization(name="院", org_type="village", level="village"))
        db.commit()
    items = client.get("/api/exams/recognition-items", headers=admin)
    assert items.status_code == 200, items.text[:200]
    assert [r["item_name"] for r in items.json() if r["item_code"] == "P240-EMPTY"] == [""]
    orgs = client.get("/api/organizations", headers=admin)
    assert orgs.status_code == 200, orgs.text[:200]
    assert "院" in [o["name"] for o in orgs.json()]
