"""整数 / 金额入参没有上限就写进 `Integer` / `Money` 列：开发库照存，生产库溢出即 500（P1-93）。

写接口把请求体的整数写进 `Integer` 列（PostgreSQL `integer`，±2147483647）、把金额写进 `Money`
（`Numeric(14, 2)`，12 位整数），入参却只有下界（`ge=0` / `gt=0`）或干脆没有界：

- **开发库（SQLite）**：整数是 8 字节、NUMERIC 不限精度，照存——开发、测试一律绿；
- **生产库（PostgreSQL）**：`integer out of range` / `numeric field overflow`，没人接，整个请求 **500**。

真 PG 实测（修前代码）：满意度评价 `target_id=99999999999` → 500；收费项目 `price=1e13` → 500。

**本文件**：①棘轮——这种「入参挡不住越过列容量 → 定长数值列」只减不增（写库形状与字符串那一族共用
`test_body_str_length.body_column_writes`，含请求体列表子项逐个写库）；②修过的端点的回归：超出列容量 422，
恰好到上限照常收。修法：字段补 `le=INT4_MAX` / `le=MONEY_MAX`（`app/numtypes.py`），没有下界的补对称的下界。
**外键列不在此列**——引用存在性由 P1-90 管（PG 上按主键查一个超大 id 只是查不到，404）。

**生产库那一半要在真 PG 上跑才算数**：`tests/test_postgres_real.py` 末条用 `MEDPLAT_NUMCAP_PG_URL`
把本文件换到 PG 上再跑一遍。
"""
import os
import typing

# 引擎是模块级的，`app.database` 一旦导入就定型——切库必须赶在导入之前。
_PG_URL = os.environ.get("MEDPLAT_NUMCAP_PG_URL", "")
if _PG_URL:
    os.environ["MEDPLAT_DATABASE_URL"] = _PG_URL

import pytest  # noqa: E402
import test_body_str_length as strlen  # noqa: E402

from app.database import engine  # noqa: E402
from app.numtypes import INT4_MAX, INT4_MIN, MONEY_MAX  # noqa: E402

if _PG_URL:
    assert engine.dialect.name == "postgresql", (
        "MEDPLAT_NUMCAP_PG_URL 已给出，引擎却不是 PostgreSQL——"
        f"实际 {engine.dialect.name}，多半是 app.database 在本模块之前就被导入了"
    )

#: 基线：已清零，此后即零基线闸门（`scripts/dump_gate_status.py` 把它列进闸门现状）。
#: 2026-09-24 实测 65 处（非外键 `Integer` 48、`Money` 17；两个列同名的字段写进两张表算两处）→ 0（同批补齐：
#: 64 个字段补 `le=INT4_MAX` / `le=MONEY_MAX`，一个界都没有的 8 个多态 id / 序号补对称的 `ge=INT4_MIN`）。
#: 第三层（同日）：写库形状补上「取出来的对象上显式赋值」，又量出签合同金额 1 处，同批补齐，仍为 0。
#: 第四层（同日）：写库形状再补三处盲区（见 `test_body_str_length.body_column_writes`），又量出 3 处——调度任务
#: 周期、药品库存预警阈值、手术出血量；同批补齐（入库数量走原子累加 `add_amount`，判据看不见，一并补上），仍为 0。
#: 判据盲区（同日）：`FiniteFloat | None` 这类 Union 里的 Annotated 原先不算数值（`_is_number`），又量出收费项目改价
#: 1 处；同一端点族的调价 `RepriceIn.new_price` 经 `_change_price` 写库、判据看不见，一并补上，仍为 0。
BASELINE = 0


def _column_caps() -> dict[str, dict[str, tuple[float, float, str]]]:
    """ORM 模型 → {列: (下限, 上限, 类型)}：非主键、非外键的 Integer 与 Numeric 列。"""
    import app.models  # noqa: F401  先平台再 spd，见 P2-51
    import app.spd.models  # noqa: F401
    from sqlalchemy import BigInteger, Float, Integer, Numeric, SmallInteger

    from app.database import Base

    caps: dict[str, dict[str, tuple[float, float, str]]] = {}
    for mapper in Base.registry.mappers:
        cols = {}
        for c in mapper.columns:
            t = c.type
            if c.primary_key or c.foreign_keys or isinstance(t, Float):
                continue
            if isinstance(t, SmallInteger):
                cols[c.name] = (-32_768, 32_767, "SmallInteger")
            elif isinstance(t, BigInteger):
                cols[c.name] = (-(2 ** 63), 2 ** 63 - 1, "BigInteger")
            elif isinstance(t, Integer):
                cols[c.name] = (INT4_MIN, INT4_MAX, "Integer")
            elif isinstance(t, Numeric) and t.precision:
                top = 10 ** (t.precision - (t.scale or 0)) - 10 ** -(t.scale or 0)
                cols[c.name] = (-top, top, f"Numeric({t.precision},{t.scale or 0})")
        caps[mapper.class_.__name__] = cols
    return caps


def _is_number(field) -> bool:
    ann = field.annotation
    # `FiniteFloat | None` 这类：顶层的 Annotated 会被 pydantic 拆进 metadata，Union 里的不会——得自己剥一层，
    # 否则可空的有限浮点整个不算数值（收费项目改价 `ChargeItemUpdate.price` 曾因此漏在判据之外）
    variants = {typing.get_args(a)[0] if typing.get_origin(a) is typing.Annotated else a
                for a in (ann, *typing.get_args(ann))}
    return bool({int, float} & variants) and bool not in variants


def _bounds(field) -> tuple[float | None, float | None]:
    """字段自己挡得住的 (下界, 上界)；`gt` / `lt` 按开区间算，没有就是 None。"""
    ann = field.annotation
    metas = list(field.metadata) + [m for a in (ann, *typing.get_args(ann)) for m in getattr(a, "__metadata__", ())]
    lows = [getattr(m, k) for m in metas for k in ("ge", "gt") if getattr(m, k, None) is not None]
    highs = [getattr(m, k) for m in metas for k in ("le", "lt") if getattr(m, k, None) is not None]
    return (max(lows) if lows else None), (min(highs) if highs else None)


def uncapped_body_numbers(modules=None) -> list[str]:
    """`模块:请求模型.字段→ORM模型.列[类型]`：写接口把这个数值字段写进定长数值列，入参却挡不住越过列容量。"""
    caps = _column_caps()
    found = set()
    for modname, cls, field_name, model, column in strlen.body_column_writes(modules):
        cap = caps.get(model, {}).get(column)
        field = cls.model_fields[field_name]
        if cap is None or not _is_number(field):
            continue
        low, high = _bounds(field)
        if low is not None and high is not None and low >= cap[0] and high <= cap[1]:
            continue
        found.add(f"{modname.removeprefix('app.')}:{cls.__name__}.{field_name}→{model}.{column}[{cap[2]}]")
    return sorted(found)


def test_数值入参无容量上限写进定长列_只减不增():
    bad = uncapped_body_numbers()
    assert len(bad) <= BASELINE, (
        f"入参挡不住越过列容量、却写进定长数值列的字段 {len(bad)} 处，超过基线 {BASELINE}：\n  "
        + "\n  ".join(bad)
        + "\n\n生产库（PG）上溢出即 500。给字段补 `le=INT4_MAX` / `le=MONEY_MAX`（`app/numtypes.py`），"
        "没有下界的补对称的下界。"
    )


def test_修完请把基线调小():
    assert len(uncapped_body_numbers()) >= BASELINE, (
        f"实测 {len(uncapped_body_numbers())} 处，比基线 {BASELINE} 少——把 BASELINE 调小并写上是哪一批"
    )


def test_判据自证_只有下界_没有界_挡得住的不报():
    import types

    from pydantic import BaseModel, Field, FiniteFloat

    snippet = '''
class LineIn(BaseModel):
    days: int = Field(default=1, ge=1)

class NoteIn(BaseModel):
    patient_id: int
    score: int = Field(ge=1)
    bounded: int = Field(ge=1, le=100)
    lines: list[LineIn] = []

class PricePatch(BaseModel):
    price: FiniteFloat | None = Field(default=None, gt=0)

@router.post("/n")
def create(body: NoteIn, db=None):
    db.add(SatisfactionSurvey(target_id=body.score, score=body.bounded, patient_id=body.patient_id))
    for line in body.lines:
        db.add(PrescriptionItem(**line.model_dump()))

@router.patch("/p")
def patch(i: int, body: PricePatch, db=None):
    item = db.get(ChargeItem, i)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
'''

    class _Router:
        def __getattr__(self, _name):
            return lambda *a, **k: (lambda fn: fn)

    mod = types.ModuleType("自证")
    mod.__dict__.update({"BaseModel": BaseModel, "Field": Field, "FiniteFloat": FiniteFloat, "router": _Router()})
    exec(compile(snippet, "自证", "exec"), mod.__dict__)
    got = uncapped_body_numbers([("自证", mod, snippet)])
    # patient_id 是外键列（归 P1-90 管），bounded 上下界都在列容量里：都不报；可空的有限浮点照样算数值
    assert got == ["自证:LineIn.days→PrescriptionItem.days[Integer]",
                   "自证:NoteIn.score→SatisfactionSurvey.target_id[Integer]",
                   "自证:PricePatch.price→ChargeItem.price[Numeric(14,2)]"], got


# ================================================================ 回归：超出列容量 422，恰好到上限照常收
@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", json={"name": "列容量卫生院", "org_type": "township",
                                                  "level": "township"}, headers=admin).json()["id"]
    pid = client.post("/api/patients", json={"name": "列容量患者", "id_card": "330191198001010193",
                                             "gender": "男", "birth_date": "1980-01-01"}, headers=admin).json()["id"]
    return {"org": org, "patient": pid}


@pytest.mark.parametrize("target_id", [INT4_MAX + 1, INT4_MIN - 1, 99999999999])
def test_满意度评价对象号越过integer_422而不是生产库500(client, admin, world, target_id):
    r = client.post("/api/surveys", json={"target_type": "encounter", "target_id": target_id,
                                          "patient_id": world["patient"], "score": 5}, headers=admin)
    assert r.status_code == 422 and "target_id" in r.text, (target_id, r.status_code, r.text[:200])


@pytest.mark.parametrize("target_id", [INT4_MAX, INT4_MIN])
def test_满意度评价对象号恰好到integer边界照常收(client, admin, world, target_id):
    r = client.post("/api/surveys", json={"target_type": "encounter", "target_id": target_id,
                                          "patient_id": world["patient"], "score": 5}, headers=admin)
    assert r.status_code == 201, (target_id, r.status_code, r.text[:200])


def test_收费项目单价越过Money_422_恰好到上限照常收(client, admin):
    r = client.post("/api/billing/charge-items", json={"code": "P193BIG", "name": "列容量", "price": 1e13},
                    headers=admin)
    assert r.status_code == 422 and "price" in r.text, (r.status_code, r.text[:200])
    r = client.post("/api/billing/charge-items", json={"code": "P193MAX", "name": "列容量", "price": MONEY_MAX},
                    headers=admin)
    assert r.status_code == 201 and r.json()["price"] == MONEY_MAX, (r.status_code, r.text[:200])


def test_判据盲区_收费项目改价与调价越过Money_422_恰好到上限照常收(client, admin):
    """`ChargeItemUpdate.price` 是 `FiniteFloat | None`，判据原先不认它是数值；调价 `RepriceIn.new_price` 经
    `_change_price` 写库，判据看不见 helper 里的写入——两处都只有下界，修前 SQLite 照收、PG 上 500。"""
    item = client.post("/api/billing/charge-items", json={"code": "P193FIX", "name": "改价容量", "price": 10},
                       headers=admin)
    assert item.status_code == 201, item.text
    url = f"/api/billing/charge-items/{item.json()['id']}"
    r = client.patch(url, json={"price": 1e13}, headers=admin)
    assert r.status_code == 422 and "price" in r.text, (r.status_code, r.text[:200])
    r = client.post(f"{url}/reprice", json={"new_price": 1e13, "reason": "越界"}, headers=admin)
    assert r.status_code == 422 and "new_price" in r.text, (r.status_code, r.text[:200])
    r = client.patch(url, json={"price": MONEY_MAX}, headers=admin)
    assert r.status_code == 200 and r.json()["price"] == MONEY_MAX, (r.status_code, r.text[:200])
    r = client.post(f"{url}/reprice", json={"new_price": 12.5, "reason": "恰好在界内"}, headers=admin)
    assert r.status_code == 200 and r.json()["price"] == 12.5, (r.status_code, r.text[:200])


def test_处方明细天数越过integer_422(client, admin, world):
    """请求体列表子项逐个写库（`body_column_writes` 的第四种形状）同样要挡住。"""
    item = {"drug_code": "P193RX", "drug_name": "列容量药", "daily_dose": 1, "days": INT4_MAX + 1}
    r = client.post("/api/prescriptions", json={"patient_id": world["patient"], "org_id": world["org"],
                                                "items": [item]}, headers=admin)
    assert r.status_code == 422 and "days" in r.text, (r.status_code, r.text[:200])


def test_批量号源模板容量越过integer_422(client, admin, world):
    template = {"resource_type": "outpatient", "resource_name": "列容量门诊", "capacity": INT4_MAX + 1}
    r = client.post("/api/appointments/slots/batch",
                    json={"org_id": world["org"], "date_from": "2031-04-04", "date_to": "2031-04-04",
                          "templates": [template]}, headers=admin)
    assert r.status_code == 422 and "capacity" in r.text, (r.status_code, r.text[:200])


def test_第三层_签合同金额越过Money_422(client, admin, world):
    """`purchase.contract_amount = body.contract_amount`——取出来的对象上显式赋值（第五种形状）同样要挡住。"""
    from conftest import login

    # 申请人不得自批：申请由本院经办提、admin 审批
    r = client.post("/api/users", json={"username": "p193_op", "password": "passw0rd1", "full_name": "列容量经办",
                                        "role": "operator", "org_id": world["org"]}, headers=admin)
    assert r.status_code == 201, r.text
    op = login(client, "p193_op", "passw0rd1")
    r = client.post("/api/materials/purchases", json={"org_id": world["org"], "item_name": "列容量设备"},
                    headers=op)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert client.post(f"/api/materials/purchases/{pid}/approve", json={"approved": True},
                       headers=admin).status_code == 200
    supplier = client.post("/api/pharmacy/suppliers", json={"name": "列容量供应商"}, headers=admin)
    assert supplier.status_code == 201, supplier.text
    body = {"supplier_id": supplier.json()["id"], "contract_no": "P193-C1", "contract_amount": 1e13}
    r = client.post(f"/api/materials/purchases/{pid}/contract", json=body, headers=admin)
    assert r.status_code == 422 and "contract_amount" in r.text, (r.status_code, r.text[:200])
    r = client.post(f"/api/materials/purchases/{pid}/contract", json={**body, "contract_amount": MONEY_MAX},
                    headers=admin)
    assert r.status_code == 200, (r.status_code, r.text[:200])


def test_第四层_调度周期与库存阈值数量越过integer_422(client, admin, world):
    """按任务名 / 业务键查出来再改的对象，原先判据看不见；入库数量走原子累加 `add_amount`，同样补上界。"""
    name = client.get("/api/jobs", headers=admin).json()[0]["name"]
    r = client.patch(f"/api/jobs/{name}", json={"interval_seconds": INT4_MAX + 1}, headers=admin)
    assert r.status_code == 422 and "interval_seconds" in r.text, (r.status_code, r.text[:200])
    base = {"org_id": world["org"], "drug_code": "P193-4", "drug_name": "第四层药", "quantity": 1}
    for field in ("threshold", "quantity"):
        r = client.post("/api/pharmacy/stocks", json={**base, field: INT4_MAX + 1}, headers=admin)
        assert r.status_code == 422 and field in r.text, (field, r.status_code, r.text[:200])
