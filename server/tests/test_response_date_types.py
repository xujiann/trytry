"""出参模型不带入参的严格日期类型（P1-63）。

D-3 与 P1-61 把请求模型的日期字段换成 `DateStr` / `OptionalDateStr`（月度的 `PeriodStr` 同理），
挡的是**入口**。可有 7 个响应模型写成 `class XxxOut(XxxCreate)`——继承下来的不只是字段，
还有那道日历校验。FastAPI 会拿 `response_model` 校验出参，于是：

- 换类型之前入的库（`Field(pattern=...)` 放行过 `2026-02-31`，裸 `str` 什么都放行过），
  或经导入脚本、直改库进来的坏日期，读出来时在**出参**校验上失败；
- 失败的是整个响应：库里一条 `duty_date="2026-02-31"`，`GET /api/mgmt/rosters`
  **整张排班表 500**（实测）。体检总检更糟：结论已经 commit，响应却是 500，前端按失败处理。

出参的职责是把库里有什么原样交出来——坏日期要在列表里**看得见**，才谈得上改；
入口校验放在出参上只能变成 500，调用方拿它什么也做不了。

改法：7 个出参模型把继承来的严格字段覆盖回 `str`（pydantic v2 覆盖不改字段顺序，
默认值照旧，响应字节不变）。本文件两件事：

1. **守卫**（派生、无豁免名单）：所有路由的 `response_model` 顺着嵌套模型一路查下去，
   不得带 `datetypes` 里的任何严格类型（从模块里推导，以后新加的类型自动纳入）。
   P1-61 还要继续把请求模型换成严格类型，而剩下的字段里有 6 个出参是继承来的
   （2026-09-24：`QcOut` / `AssessmentOut` / `WomenHealthOut` / `MonitorOut` /
   `ContractOut` / `PatientOut`）——换的那一批这里就红，逼着同一批把出参覆盖回来。
2. **回归**：直接往库里插存量坏日期，逐个读接口断言 200 且坏值原样读出。
"""
from __future__ import annotations

import typing

import pytest
import test_api_contract_governance as contract
from pydantic import BaseModel, BeforeValidator

from app import datetypes
from app.database import SessionLocal
from app.models import (
    AppointmentSlot,
    ChildRecord,
    DutyRoster,
    InfectiousCase,
    PhysicalExam,
    VaccinationRecord,
)


def _strict_validators() -> dict[object, str]:
    """`datetypes` 里每个严格类型的校验函数 → 类型名。从模块推导，不手抄。"""
    found: dict[object, str] = {}
    for name, value in vars(datetypes).items():
        if typing.get_origin(value) is typing.Annotated:
            for meta in typing.get_args(value)[1:]:
                if isinstance(meta, BeforeValidator):
                    found[meta.func] = name
    return found


STRICT = _strict_validators()


def _strict_fields(tp, seen: set | None = None) -> list[tuple[str, str]]:
    """`tp` 里（顺着 list/dict/Optional/嵌套模型）带严格类型的字段：[(模型.字段, 类型名)]。"""
    seen = set() if seen is None else seen
    hits: list[tuple[str, str]] = []
    if typing.get_origin(tp) is typing.Annotated:
        base, *metas = typing.get_args(tp)
        hits += [("", STRICT[m.func]) for m in metas
                 if isinstance(m, BeforeValidator) and m.func in STRICT]
        return hits + _strict_fields(base, seen)
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        if tp in seen:
            return hits
        seen.add(tp)
        for name, info in tp.model_fields.items():
            where = f"{tp.__module__}:{tp.__qualname__}.{name}"
            hits += [(where, STRICT[m.func]) for m in info.metadata
                     if isinstance(m, BeforeValidator) and m.func in STRICT]
            hits += [(where, kind) for _, kind in _strict_fields(info.annotation, seen)]
        return hits
    for arg in typing.get_args(tp):
        hits += _strict_fields(arg, seen)
    return hits


def test_严格类型是从datetypes推导出来的_不是空集():
    assert {"DateStr", "OptionalDateStr", "PeriodStr", "OptionalPeriodStr"} <= set(STRICT.values())


def test_判据自证_继承_嵌套_列表_可空都认得出():
    class _In(BaseModel):
        day: datetypes.DateStr

    class _Out(_In):  # 本条要抓的形状：出参继承入参
        id: int

    class _Page(BaseModel):
        rows: list[_Out]
        days: list[datetypes.OptionalDateStr] = []
        period: datetypes.PeriodStr | None = None

    class _Fixed(_In):  # 修法：覆盖回 str
        id: int
        day: str

    assert [kind for _, kind in _strict_fields(_Out)] == ["DateStr"]
    assert sorted(kind for _, kind in _strict_fields(dict[str, list[_Page]])) == [
        "DateStr", "OptionalDateStr", "PeriodStr"]
    assert _strict_fields(_Fixed) == []
    assert list(_Fixed.model_fields) == ["day", "id"], "覆盖不该挪动字段顺序（响应字节不变）"


def test_出参模型不带严格日期类型():
    offenders = sorted({
        f"{field}（{kind}）← {'/'.join(sorted(route.methods))} {route.path}"
        for _, route in contract._iter_endpoints()
        if route.response_model is not None
        for field, kind in _strict_fields(route.response_model)
    })
    assert not offenders, (
        "这些响应模型带着入参的严格日期类型——库里一条存量坏日期就会让整个响应 500。"
        "在出参模型里把字段覆盖回 `str`（字段顺序不变），见本文件开头：\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------- 存量坏日期原样读出

@pytest.fixture(scope="module")
def legacy(client, admin):
    """每张表插一条日期写坏了的存量行（绕过接口，模拟换类型之前入的库）。"""
    org = client.post("/api/organizations",
                      json={"name": "出参日期回归院", "org_type": "township", "level": "township"},
                      headers=admin).json()
    patient = client.post("/api/patients",
                          json={"name": "出参日期回归患者", "id_card": "330281199012124514"},
                          headers=admin).json()
    rows = {
        # 坏日期放在将来的年份：排班表不带日期时只给今天及以后（P2-208），放在过去就读不到了——要钉的是「坏值原样读出」
        "roster": DutyRoster(center_type="出参回归中心", duty_date="2099-02-31", doctor_name="值班甲"),
        "exam": PhysicalExam(patient_id=patient["id"], org_id=org["id"], exam_date="2026/09/24"),
        "child": ChildRecord(name="出参回归儿童", birth_date="20260924"),
        "vaccination": VaccinationRecord(patient_id=patient["id"], vaccine_code="HepB",
                                         vaccine_name="乙肝疫苗", vaccinated_date="2026-02-30",
                                         org_id=org["id"]),
        "case": InfectiousCase(org_id=org["id"], disease_code="P163", disease_name="回归病种",
                               onset_date="2026-13-01"),
        "slot": AppointmentSlot(org_id=org["id"], resource_type="outpatient",
                                resource_name="出参回归诊室", slot_date="2026-02-29"),
    }
    with SessionLocal() as db:
        db.add_all(rows.values())
        db.commit()
        ids = {key: row.id for key, row in rows.items()}
    return {"org_id": org["id"], "patient_id": patient["id"], **ids}


#: (读接口, 查询参数里要填的 id, 日期字段, 插进去的坏值)
LIST_CASES = [
    ("/api/mgmt/rosters", {"center_type": "出参回归中心"}, "duty_date", "2099-02-31"),
    ("/api/checkups", {"patient_id": "patient_id"}, "exam_date", "2026/09/24"),
    ("/api/maternal/children", {}, "birth_date", "20260924"),
    ("/api/vaccination/records", {"patient_id": "patient_id"}, "vaccinated_date", "2026-02-30"),
    ("/api/infectious/cases", {"disease_code": "P163"}, "onset_date", "2026-13-01"),
    ("/api/appointments/slots", {"org_id": "org_id"}, "slot_date", "2026-02-29"),
]


@pytest.mark.parametrize("path, params, field, value", LIST_CASES, ids=[c[2] for c in LIST_CASES])
def test_列表里有一条存量坏日期_整个列表照常返回(client, admin, legacy, path, params, field, value):
    query = {k: legacy.get(v, v) for k, v in params.items()}
    resp = client.get(path, params=query, headers=admin)
    assert resp.status_code == 200, (path, resp.status_code, resp.text[:200])
    assert value in [row[field] for row in resp.json()], f"{path} 的坏值应原样读出，看得见才改得了"


def test_体检总检_存量坏日期不让已提交的结论回500(client, admin, legacy):
    resp = client.post(f"/api/checkups/{legacy['exam']}/review",
                       json={"final_conclusion": "未见明显异常"}, headers=admin)
    assert resp.status_code == 200, resp.text[:200]
    assert resp.json()["exam_date"] == "2026/09/24"
    assert resp.json()["final_conclusion"] == "未见明显异常"
