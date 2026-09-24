"""数值入参的列容量上限：请求体里的整数 / 金额写进 `Integer` / `Money` 列之前，入参得先挡住列装不下的值（P1-93）。

与 `datetypes.py` 同一个角色：入参类型约束的唯一出处，平台与慢专病两边的路由都从这里取，不各写一个魔数。

- `INT4_MIN` / `INT4_MAX`：PostgreSQL `integer` 的取值范围。SQLite 的整数是 8 字节，开发库照存；
  生产库超出即 `integer out of range`，整个请求 500。
- `MONEY_MAX`：`Money = Numeric(14, 2)`（`models/_base.py`）装得下的最大值——12 位整数 + 2 位小数。
  超出即 `numeric field overflow`。

用法：`quantity: int = Field(ge=1, le=INT4_MAX)`、`price: FiniteFloat = Field(gt=0, le=MONEY_MAX)`。
只是列容量，不是业务上限——业务上合理的范围（一次领药最多几盒）另议，别拿这两个数冒充。
"""

INT4_MIN = -2_147_483_648
INT4_MAX = 2_147_483_647
MONEY_MAX = 999_999_999_999.99
