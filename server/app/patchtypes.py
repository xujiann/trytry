"""改档（PATCH）请求模型的共用写法：不可空的列「可以不传、不能传 null」（P1-94）。

改档要分清三种输入：不传（不改）、传值（改成它）、传 null（清空）。可空的列三种都合法，照常写
`T | None = None`；**不可空的列只有前两种**——显式传 null 写进去，就是 `NOT NULL` 约束失败、500。

写法：字段仍声明成 `T`（不带 `| None`），默认值给 `UNSET`::

    name: str = Field(default=UNSET, min_length=1, max_length=64)

- 不传：取默认值且不经校验（pydantic 默认不校验默认值），`model_dump(exclude_unset=True)` 里没有它，不改；
- 传值：照常过类型与约束；
- 传 null：过不了 `T` 的类型校验，字段级 422。

`UNSET` 就是 `None`，只是类型标成 `Any`：pydantic 的 `Field(default=...)` 按默认值推断返回类型，
直接写 `Field(default=None)` 会被类型检查器判成「把 None 赋给 str」。

与 `datetypes.py` / `numtypes.py` 同一个角色：入参写法的唯一出处，平台与慢专病两边都从这里取。
"""
from typing import Any

UNSET: Any = None
