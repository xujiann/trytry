"""日期入参类型（D-3）。

在此之前全平台 22 处日期字段用 `Field(pattern=r"^\\d{4}-\\d{2}-\\d{2}$")` 校验。
正则只管形状不管日历，`2026-02-31` 能过；入库之后各处统计 `strptime` 解析失败
就 `continue` 或返回 0——实测用假日期建的一条派驻，整条从下沉指标里消失了。

这直接违反平台反复坚持的原则：**未采集/异常的数据要单独报出来，不能悄悄丢掉**
（DDD 未维护单列、职称等级未填单列、疗效未评价单列，都是这个原则）。
偏偏日期这里，一条记录无声无息地不见了。

改法是在入口就挡下来：先卡死 `YYYY-MM-DD` 的形状（`date.fromisoformat` 在
Python 3.11 上还接受 `20260212`、`2026-W07-1` 等 ISO 变体，对接方按形状写死的
解析会崩），再用 `date.fromisoformat` 做真实日历校验。

**落库仍是字符串**：这些列都是 `String(10)`，改表结构会牵动 40 多张表的迁移与
全部按字符串比较的查询（日期字符串的字典序与时序一致，大量 `>=`/`<=` 依赖它）。
在模型层挡住非法值即可，表结构不动。
"""
import re
from datetime import date
from typing import Annotated

from pydantic import BeforeValidator

_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _check(value: object, *, allow_blank: bool) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        return value  # 交给 pydantic 报类型错
    if value == "":
        if allow_blank:
            return value
        raise ValueError("日期不能为空，格式须为 YYYY-MM-DD")
    if not _SHAPE.match(value):
        raise ValueError("日期格式须为 YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"日期 {value} 不存在（请检查月份天数）") from None
    return value


def _required(value: object) -> object:
    return _check(value, allow_blank=False)


def _optional(value: object) -> object:
    return _check(value, allow_blank=True)


#: 必填日期，`YYYY-MM-DD`，做真实日历校验
DateStr = Annotated[str, BeforeValidator(_required)]
#: 可空日期，空串表示未填；非空则同样做日历校验
OptionalDateStr = Annotated[str, BeforeValidator(_optional)]
