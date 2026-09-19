"""日期与月份入参类型（D-3 / P1-34）——全平台日期口径的唯一真源。

前半是日期（`YYYY-MM-DD`，D-3），后半是统计周期（`YYYY-MM`，P1-34）。
两者是同一个坑的两半：正则只管形状不管日历，非法值静默入库，然后在统计里
被解析失败的分支悄悄丢掉。谁也不报错，报表就少一段。

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


# --------------------------------------------------------------- 月份口径（P1-34）
#
# `YYYY-MM` 的统计周期是同一个坑的另一半：5 处入参各写一遍
# `pattern=r"^\d{4}-\d{2}$"`，正则只管形状不管日历，`2026-13` / `2026-00` 一路放行。
# 后果与上面那条派驻同族——脏周期入库后，按 `strftime("%Y-%m")` 归属的统计里
# 永远匹配不到任何一条记录，报表安静地少一段；按 `period` 等值查的薪酬/财务
# 则凭空多出一期再也对不回去的数。两边都不报错。
#
# 这里不复用 `DateStr`（补个 `-01` 再走它）：错误文案会变成"日期…"，而入参名字
# 叫 period，对接方收到的提示必须说的是周期。判定共享，措辞分开。
#
# **不接受 `date` 实例**（`DateStr` 接受）：把某一天截成它所在的月份是有损的，
# 调用方以为传的是"这一天"，落库的却是"这个月"，出错时无从分辨。要月份就传月份。

#: 只认半角数字。`\d` 会放行全角（"２０２６-０１"）——那种值形状"对"却不是
#: 合法年份，`deps.period_bounds` 早就踩过这一条（见那里的注释）。
#: 这条形状是全平台月份口径的**唯一真源**，`deps` 直接复用它，别处不得再抄一份。
PERIOD_SHAPE = re.compile(r"^[0-9]{4}-[0-9]{2}$")

#: 受理的年份区间。上界照抄仓库已有的年份口径（`fund.PoolIn.year` 是
#: `Field(ge=2000, le=2100)`，CLAUDE.md §4"照抄现状，别自创"），顺带挡住
#: `9999-12` 这一类**日历合法但次月首日溢出 `datetime.max`** 的值——下游
#: （`fund._collect_expense` / `deps.month_bounds`）普遍要算次月首日。
#: 下界没跟着取 2000：`/api/quality/records/qc-summary?period=1999-01` 今天
#: 返回 200 的空统计（`tests/test_medical_record_qc.py` 钉着），收紧它属于改既有
#: 响应字节（CLAUDE.md 第 7 条），不在"非法月份改判 422"这个缺陷修复的授权范围内。
#: 1900 已足够挡住 `0000`/`0001`/`1899` 这类只可能来自手滑或拼串的年份。
PERIOD_MIN_YEAR = 1900
PERIOD_MAX_YEAR = 2100


def _check_period(value: object, *, allow_blank: bool) -> object:
    if not isinstance(value, str):
        return value  # 交给 pydantic 报类型错
    if value == "":
        if allow_blank:
            return value
        raise ValueError("统计周期不能为空，格式须为 YYYY-MM")
    if not PERIOD_SHAPE.match(value):
        raise ValueError("统计周期格式须为 YYYY-MM")
    try:
        # 补 `-01` 借 `date.fromisoformat` 做日历校验：月份 00 与 13 都在这里被拒。
        first = date.fromisoformat(value + "-01")
    except ValueError:
        raise ValueError(f"统计周期 {value} 不存在（月份须为 01–12）") from None
    if not PERIOD_MIN_YEAR <= first.year <= PERIOD_MAX_YEAR:
        raise ValueError(
            f"统计周期 {value} 的年份超出受理范围 {PERIOD_MIN_YEAR}–{PERIOD_MAX_YEAR}"
        )
    return value


def _period_required(value: object) -> object:
    return _check_period(value, allow_blank=False)


def _period_optional(value: object) -> object:
    return _check_period(value, allow_blank=True)


def is_period(value: str) -> bool:
    """查询参数用的谓词：这个字符串是不是一个合法的 `YYYY-MM` 统计周期。

    存在的理由是**出口不同，判定必须相同**。`PeriodStr` 只能装在 pydantic 模型的
    字段上；`period` 作为查询参数出现时（`quality` / `reports`），既有端点是自己
    `raise HTTPException(422, detail="period 格式须为 YYYY-MM")`——那句文案是既有
    响应体，改了就是破坏兼容（CLAUDE.md 第 7 条）。所以这里只把**判定**借出去，
    让调用方保留自己的措辞，而不是让它们各自再写一遍正则。
    """
    try:
        _check_period(value, allow_blank=False)
    except ValueError:
        return False
    return True


#: 必填统计周期，`YYYY-MM`，做真实日历校验（月份 01–12）与年份区间校验
PeriodStr = Annotated[str, BeforeValidator(_period_required)]
#: 可空统计周期，空串表示未填；非空则同样做日历与年份校验
OptionalPeriodStr = Annotated[str, BeforeValidator(_period_optional)]
