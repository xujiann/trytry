"""金额入参多于两位小数：开发库照存，生产库 `Numeric(14, 2)` 悄悄四舍五入（P2-65）。

`Money = Numeric(14, 2)`（`models/_base.py`）的设计口径是「精确到分」，可写这些列的请求字段只挡了上下界
（`gt=0` / `le=MONEY_MAX`），不挡小数位：

- **开发库（SQLite）**：NUMERIC 不限小数位，12.345 原样存、原样算——开发、测试一律照「录的是多少就是多少」；
- **生产库（PostgreSQL）**：`numeric(14,2)` 在写入时四舍五入，**不报错**。

2026-09-25 真 PG 实测（修前代码，收费项目目录）：单价录 12.345 → 存成 12.35；录 0.0325 → 0.03；
录 0.004 → **0.00**——`gt=0` 形同虚设，目录里多出一个零价收费项目。按这个项目计费 ×10：生产库 123.5、
开发库 123.45，同一笔账两边算出不同的数，而用户录的是第三个数。

修法：`app.numtypes.MoneyFloat`——多于两位小数的 422「最多保留两位小数」；二进制浮点噪声（`0.1 + 0.2`）
归整到分再落库，两库存的是同一个数；合法的两位小数原样返回、字节不变。

**本文件**：①派生零基线闸门——写进小数位为 2 的 `Numeric` 列的请求字段都得带 `to_fen`（写库形状与
字符串那一族共用 `test_body_str_length.body_column_writes`）；②出参不许带它（修之前存进开发库的三位小数行
要照样读得出来）；③修过的端点的回归。
"""
import re
import typing

import pytest
import test_blank_required_text as blanktext
import test_body_numeric_capacity as numcap
import test_body_str_length as strlen
from pydantic import BaseModel, Field

#: 零基线（`scripts/dump_gate_status.py` 把它列进闸门现状）。
#: 2026-09-25 实测 38 处（37 个字段；调价 `RepriceIn.new_price` 同时写目录价与调价记录两列）→ 0，同批补齐。
BASELINE = 0
OUTPUT_BASELINE = 0


def _validators(field) -> list:
    """字段身上的全部元数据（含 `X | None` 里被 Union 包着、pydantic 不会替你拆开的那层 Annotated）。"""
    ann = field.annotation
    metas = list(field.metadata)
    for a in (ann, *typing.get_args(ann)):
        metas += list(getattr(a, "__metadata__", ()))
    return metas


def _carries_fen(field) -> bool:
    from app.numtypes import to_fen

    return any(getattr(m, "func", None) is to_fen for m in _validators(field))


def unrounded_money_fields(modules=None) -> list[str]:
    """`模块:请求模型.字段→ORM模型.列[类型]`：写进两位小数定点列，入参却不挡多出来的小数位。"""
    caps = numcap._column_caps()
    found = set()
    for modname, cls, field_name, model, column in strlen.body_column_writes(modules):
        cap = caps.get(model, {}).get(column)
        if cap is None or not cap[2].startswith("Numeric"):
            continue
        if cap[2].endswith(",2)") and _carries_fen(cls.model_fields[field_name]):
            continue
        # 小数位不是 2 的定点列一律点名：to_fen 只管到分，那种列得另写一个校验
        found.add(f"{modname.removeprefix('app.')}:{cls.__name__}.{field_name}→{model}.{column}[{cap[2]}]")
    return sorted(found)


def fen_outputs() -> list[str]:
    """出参模型里带 `to_fen` 的字段（多半是继承了请求模型）。"""
    return sorted(f"{cls.__module__.removeprefix('app.')}:{cls.__qualname__}.{f}"
                  for cls in blanktext.response_models() for f, info in cls.model_fields.items()
                  if _carries_fen(info))


# ================================================================ 闸门
def test_写进两位小数定点列的入参挡得住多出的小数位():
    bad = unrounded_money_fields()
    assert len(bad) <= BASELINE, (
        "这些请求字段写进 Numeric(…, 2) 列，却收得下三位以上小数——生产库会悄悄四舍五入：\n  "
        + "\n  ".join(bad)
        + "\n\n注解换成 `MoneyFloat`（`from app.numtypes import MoneyFloat`），可空的写 `MoneyFloat | None`。"
    )


def test_修完请把基线调小():
    assert len(unrounded_money_fields()) >= BASELINE, "实测比基线少——把 BASELINE 调小并写上是哪一批"


def test_出参不带to_fen():
    offenders = fen_outputs()
    assert len(offenders) <= OUTPUT_BASELINE, (
        "这些出参字段带着 to_fen（多半是从请求模型继承来的）：\n  " + "\n  ".join(offenders)
        + "\n\n修之前存进开发库的三位小数行会让整个响应 500。在出参模型上按原约束覆盖回不带它的声明。"
    )


def test_覆盖面自证_判据扫得到写金额列的入参():
    """防空转：写库形状一个都认不出时，上面那条会空转成绿。"""
    caps = numcap._column_caps()
    money = {f"{m.removeprefix('app.')}:{cls.__name__}.{f}" for m, cls, f, model, col in strlen.body_column_writes()
             if caps.get(model, {}).get(col, (0, 0, ""))[2] == "Numeric(14,2)"}
    assert len(money) >= 37, sorted(money)
    assert {"routers.billing:ChargeItemCreate.price", "routers.billing:ChargeItemUpdate.price",
            "routers.billing:RefundIn.amount", "spd.routers.config.scales:PackagePatch.price",
            "routers.fund:PoolUpdate.prepay_ratio_pct"} <= money


def test_判据自证_可空与不可空两种写法都认得出():
    from app.numtypes import MoneyFloat

    class Probe(BaseModel):
        plain: MoneyFloat = Field(gt=0)
        optional: MoneyFloat | None = None
        bare: float = 0

    assert _carries_fen(Probe.model_fields["plain"])
    assert _carries_fen(Probe.model_fields["optional"])
    assert not _carries_fen(Probe.model_fields["bare"])


# ================================================================ 按名字补一道：写库形状看不见的金额入参
#: 上面那道按写库形状派生，入参经**原生 SQL 帮手**写库它看不见：押金退费把 `body.amount` 交给
#: `_atomic_deposit_deduct`（`INSERT … SELECT … WHERE 余额充足`），`DepositRefundIn.amount` 于是一直是
#: `FiniteFloat`，三位小数照收（P2-250）。按名字再扫一遍请求模型：名字像金额的浮点字段必须带 `to_fen`。
MONEY_NAME = re.compile(
    r"(^|_)(amount|price|fee|cost|balance|pay|payment|refund|deposit|salary|budget|expense|income)(_|$)")
#: 名字像金额、其实不是金额的请求字段：`模块:模型.字段` → 理由（只减不增）
NOT_MONEY: dict[str, str] = {}
#: 零基线（`scripts/dump_gate_status.py` 列进闸门现状）。2026-09-26 实测 1 处（押金退费）→ 0，同批修掉。
NAMED_BASELINE = 0


def _is_float(field) -> bool:
    ann = field.annotation
    for a in (ann, *typing.get_args(ann)):
        base = typing.get_args(a)[0] if typing.get_origin(a) is typing.Annotated else a
        if base is float:
            return True
    return False


def money_named_float_inputs(models: typing.Iterable[type] | None = None) -> list[str]:
    """请求模型里名字像金额、类型是浮点、却不带 `to_fen` 的字段（按声明它的类记名）。"""
    models = blanktext.request_models() if models is None else models
    return sorted({blanktext._owner_key(cls, f) for cls in models for f, info in cls.model_fields.items()
                   if MONEY_NAME.search(f) and _is_float(info) and not _carries_fen(info)} - set(NOT_MONEY))


def test_名字像金额的浮点入参一律带to_fen():
    bad = money_named_float_inputs()
    assert len(bad) <= NAMED_BASELINE, (
        "这些请求字段名字像金额、却收得下三位以上小数（写库走的是写库形状闸门看不见的路，比如原生 SQL 帮手）：\n  "
        + "\n  ".join(bad)
        + "\n\n注解换成 `MoneyFloat`；确实不是金额的登记进 NOT_MONEY 并写明理由（只减不增）。"
    )


def test_按名字那道的判据自证():
    from pydantic import FiniteFloat

    from app.numtypes import MoneyFloat

    class Probe(BaseModel):
        amount: float = 0
        unit_price: FiniteFloat | None = None
        insurance_pay: MoneyFloat = 0
        refund_amount: MoneyFloat | None = None
        prepay_ratio_pct: float = 0      # 比例不是金额：名字里的 pay 不在词边界上
        amount_fen: int = 0              # 整数分：不是浮点
        dose: float = 0

    assert money_named_float_inputs([Probe]) == [f"{Probe.__module__.removeprefix('app.')}:{Probe.__qualname__}.{f}"
                                                 for f in ("amount", "unit_price")]
    assert len(blanktext.request_models()) > 300   # 防空转：请求模型真被收集到了


# ================================================================ 取整规则本身
@pytest.mark.parametrize("raw", [12.34, 0.01, 0.07, 19.99, 100, 0, -12.34, 1234567.89, 999_999_999_999.99])
def test_合法的两位小数原样返回_字节不变(raw):
    from app.numtypes import to_fen

    assert to_fen(raw) == raw and repr(to_fen(raw)) == repr(float(raw))


@pytest.mark.parametrize("raw", [12.345, 0.004, 0.0325, 1e-7, 0.005, 2.675, 33.333, 999_999_999_999.995])
def test_多于两位小数的一律拒(raw):
    from app.numtypes import to_fen

    with pytest.raises(ValueError, match="最多保留两位小数"):
        to_fen(raw)


def test_二进制浮点噪声归整到分():
    from app.numtypes import to_fen

    assert to_fen(0.1 + 0.2) == 0.3   # 0.30000000000000004
    assert to_fen(1.1 * 3) == 3.3     # 3.3000000000000003


# ================================================================ 端点回归
def _charge(client, admin, code, price):
    return client.post("/api/billing/charge-items", headers=admin,
                       json={"code": code, "name": f"精确到分{code}", "category": "drug", "price": price})


@pytest.mark.parametrize("code, price", [("P265-1", 12.345), ("P265-2", 0.004), ("P265-3", 0.0325)])
def test_收费项目单价多于两位小数_422(client, admin, code, price):
    resp = _charge(client, admin, code, price)
    assert resp.status_code == 422, resp.text   # 修前 201：生产库存成 12.35 / 0.00 / 0.03
    err = resp.json()["detail"][0]
    assert err["loc"][-1] == "price" and "最多保留两位小数" in err["msg"], err


def test_收费项目单价两位小数照常收_响应原样(client, admin):
    resp = _charge(client, admin, "P265-4", 12.34)
    assert resp.status_code == 201, resp.text
    assert resp.json()["price"] == 12.34


def test_浮点噪声归整后落库_两库存的是同一个数(client, admin):
    resp = _charge(client, admin, "P265-5", 0.1 + 0.2)
    assert resp.status_code == 201, resp.text
    listed = {x["code"]: x["price"] for x in
              client.get("/api/billing/charge-items", headers=admin, params={"limit": 500}).json()}
    assert listed["P265-5"] == 0.3   # 修前开发库存 0.30000000000000004，生产库 0.30


def test_改价与调价同口径(client, admin):
    item = _charge(client, admin, "P265-6", 10).json()
    patch = client.patch(f"/api/billing/charge-items/{item['id']}", headers=admin, json={"price": 10.005})
    assert patch.status_code == 422, patch.text
    reprice = client.post(f"/api/billing/charge-items/{item['id']}/reprice", headers=admin,
                          json={"new_price": 10.005, "reason": "P265"})
    assert reprice.status_code == 422, reprice.text


def test_慢专病服务包价格同口径(client, admin):
    resp = client.post("/api/spd/service-packages", headers=admin,
                       json={"code": "p265_pkg", "name": "精确到分服务包", "price": 99.999})
    assert resp.status_code == 422, resp.text
