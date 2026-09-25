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
from typing import Annotated

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
