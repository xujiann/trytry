"""数值入参的列容量上限：请求体里的整数 / 金额写进 `Integer` / `Money` 列之前，入参得先挡住列装不下的值（P1-93）。

与 `datetypes.py` 同一个角色：入参类型约束的唯一出处，平台与慢专病两边的路由都从这里取，不各写一个魔数。

- `INT4_MIN` / `INT4_MAX`：PostgreSQL `integer` 的取值范围。SQLite 的整数是 8 字节，开发库照存；
  生产库超出即 `integer out of range`，整个请求 500。
- `MONEY_MAX`：`Money = Numeric(14, 2)`（`models/_base.py`）装得下的最大值——12 位整数 + 2 位小数。
  超出即 `numeric field overflow`。
- `MoneyFloat`：写进 `Money` 列（及同为两位小数的 `Numeric(…, 2)` 列）的入参——多于两位小数的 422（P2-65）。
  开发库的 NUMERIC 不限小数位，12.345 原样存；生产库 `numeric(14,2)` 写入时**悄悄四舍五入**、不报错：
  单价录 0.004 存成 0.00（`gt=0` 形同虚设），录 12.345 存成 12.35，同一笔计费两库算出不同的数。

用法：`quantity: int = Field(ge=1, le=INT4_MAX)`、`price: MoneyFloat = Field(gt=0, le=MONEY_MAX)`。
只是列容量与列精度，不是业务上限——业务上合理的范围（一次领药最多几盒）另议，别拿这两个数冒充。
"""
import math
from collections.abc import Sequence
from typing import Annotated, Any

from pydantic import AfterValidator, FiniteFloat

INT4_MIN = -2_147_483_648
INT4_MAX = 2_147_483_647
MONEY_MAX = 999_999_999_999.99


def to_fen(value: float) -> float:
    """金额精确到分：多于两位小数的拒收；二进制浮点噪声（`0.1 + 0.2`）归整到分。

    合法的两位小数原样返回、字节不变——`round(v * 100) / 100` 是正确舍入的除法，得到的就是那个两位小数
    最近的浮点数，与 JSON 里读进来的是同一个（穷举 0.00～100000.00 与随机抽样验过）。容差取「1e-6 分」与
    「8 个 ulp」中的大者：前者吸收常见量级的乘法误差，后者管住万亿量级——那里浮点本身已分辨不出第三位小数。
    """
    cents = value * 100
    if not math.isfinite(cents):   # 有限浮点乘 100 也会溢出成 inf，round(inf) 抛 OverflowError 就成了 500
        raise ValueError("最多保留两位小数")
    nearest = round(cents)
    if abs(cents - nearest) > max(1e-6, 8 * math.ulp(cents)):
        raise ValueError("最多保留两位小数")
    return nearest / 100


MoneyFloat = Annotated[FiniteFloat, AfterValidator(to_fen)]


def split_fen(total: float, weights: Sequence[float]) -> list[float]:
    """按权重把一笔金额分到分，各份合计分毫等于 `total`（最大余数法 / Hamilton）。

    逐份各自 `round(total * w / Σw, 2)` 会各丢 / 各多一分，合计对不上总额——实测 100 元 3 户均分分出 99.99。
    先给每份向下取整到分，剩下的零头一分一分地按小数余数从大到小补给各份；同余数按原顺序稳定，可复现。
    每份都是按比例的精确值向下或向上取整到分，不会出负数。金额与权重须非负、权重之和须大于 0（调用方先判）。
    原是医保基金结余分配（`fund.py`）里的一段，成本分摊（`cost.py`）同一个问题，抽到这里共用。
    """
    weight_sum = sum(weights)
    total_cents = round(total * 100)
    raw = [(total * (w / weight_sum)) * 100 for w in weights]
    floors = [int(x) for x in raw]  # 向下取整到分（金额非负，int() 即 floor）
    remainder = total_cents - sum(floors)  # 待补的零头分数，∈ [0, 份数)
    # 小数余数大的优先补一分；同余数按原顺序稳定，可复现
    order = sorted(range(len(weights)), key=lambda i: raw[i] - floors[i], reverse=True)
    cents = floors[:]
    for i in order[: max(remainder, 0)]:
        cents[i] += 1
    return [c / 100 for c in cents]


def non_finite_path(value: Any, root: str) -> str:
    """配置里第一个非有限的数（NaN / Infinity / -Infinity）在哪儿，如 `level_rules.metrics[0].level3`；没有返回空串（P2-466）。

    宽字典配置（分级规则、评分规则、质控规则、筛查条件、量表、服务包……）pydantic 不查里面，P1-92 的 `FiniteFloat`
    管不到；标准库 `json.loads` 又照收 `NaN` / `Infinity` 记号。它们也是 float，却和谁比都不成立——阈值永不触发、
    条件永不命中；算进分数里整张考核方案的出参编码失败（500）；`int(inf)` 抛 OverflowError（500）；PG 的 JSON 列
    干脆存不进去（500）。每个配置校验都先过这一道（`tests/test_config_dict_validated.py` 盯着）。
    """
    if isinstance(value, float) and not math.isfinite(value):
        return root
    if isinstance(value, dict):
        for key, item in value.items():
            found = non_finite_path(item, f"{root}.{key}")
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = non_finite_path(item, f"{root}[{index}]")
            if found:
                return found
    return ""
