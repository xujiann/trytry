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
    # fullmatch 而不是 match：`$` 允许末尾一个换行，"2026-02-28\n" 会先过形状、
    # 再在日历校验里以"日期不存在"被拒，文案对不上真正的毛病。
    if not _SHAPE.fullmatch(value):
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


# ---------------------------------------------------------------- 月度期间 YYYY-MM
#
# 同一个坑的月度版（P1-34）：财务/薪酬/基金预结的 `period` 与两个报表查询参数各自写着
# `^\d{4}-\d{2}$`，`2026-13` 照过——入了库就是一条永远对不上任何月份的记账，进了
# `strftime("%Y-%m") == period` 的过滤就是一个永远为空、却不报错的结果集。
# 形状用 `[0-9]` 而不是 `\d`：`\d` 认全角数字，`２０２６-１２` 会先过形状再在日历校验里
# 以一句"月份不存在"被拒，报错文案对不上真正的毛病。

#: 月度期间的形状（唯一真源；`deps.period_bounds` 也从这里取，不另写一份）
MONTH_SHAPE = re.compile(r"^[0-9]{4}-[0-9]{2}$")


def check_month(value: str) -> str:
    """校验 `YYYY-MM`：先卡形状，再用日历确认月份存在。非法时抛 ValueError（带人话）。"""
    if not MONTH_SHAPE.fullmatch(value):  # 同上：`$` 放过末尾换行，fullmatch 不放
        raise ValueError("期间格式须为 YYYY-MM")
    try:
        date.fromisoformat(value + "-01")
    except ValueError:
        raise ValueError(f"期间 {value} 不存在（月份须为 01~12）") from None
    return value


def _month_required(value: object) -> object:
    if not isinstance(value, str):
        return value  # 交给 pydantic 报类型错
    return check_month(value)


#: 必填月度期间，`YYYY-MM`，做真实日历校验（`2026-13` 不放行）。
#: 查询参数形态请用 `deps.require_month`（同一校验，报 422）。
PeriodStr = Annotated[str, BeforeValidator(_month_required)]
