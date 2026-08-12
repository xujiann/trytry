"""全平台唯一的时间入口（P0-2）。

在此之前平台里并存四套时间函数：`models.utcnow()` 返回 aware，
`scheduler._naive_utcnow()` 与 `portal._naive_utcnow()` 各自返回 naive，
另有 5 处路由直接内联 `datetime.utcnow()` / `datetime.now()`。
阶段二写 `/api/monitor/overview` 时就因为拿 aware 值去比调度器写入的 naive
`next_run_at` 直接 `TypeError`，靠全量测试才逮住。

**落库一律 naive UTC**。理由不是 naive 更好，而是平台绝大多数 `DateTime` 列
没有 `timezone=True`：SQLite 读回来 tzinfo 恒为 None，PostgreSQL 则会把
aware 值按会话时区转换后再丢掉时区——同一张表的两列一列 aware 一列 naive，
在 SQLite 上看不出问题，换库就炸。统一成 naive UTC，读回来与写进去一致。

需要给外部（接口返回、导出）标注时区的，用 `to_aware()` 在出口处转一次，
不要把 aware 值写进库。

`test_clock.py` 有一条扫描用例：`app/` 下不得再出现裸 `datetime.utcnow()`
或 `datetime.now()`，新代码一律走本模块。
"""
from datetime import date, datetime, timezone

__all__ = ["now_naive", "now_aware", "now_local", "to_aware", "today", "today_str"]


def now_naive() -> datetime:
    """当前 UTC 时刻，naive。**落库一律用这个。**"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def now_aware() -> datetime:
    """当前 UTC 时刻，带时区。仅用于对外输出与跨时区计算。"""
    return datetime.now(timezone.utc)


def now_local() -> datetime:
    """当前本地时刻，naive。**只用于给人看的字符串**（打印页脚、记录时间默认值），
    不要落库参与比较——本地时区一变，历史值与新值就不在同一把尺子上。
    """
    return datetime.now()


def to_aware(value: datetime | None) -> datetime | None:
    """把库里读出的 naive UTC 值标上时区。已带时区的原样返回。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def today() -> date:
    """当前业务日期（本地时区）。

    刻意**不用 UTC**：随访超期、暂存滞留、排班这些都是按当地日历算的，
    UTC 会让东八区的用户在早上 8 点前看到"昨天"。
    """
    return date.today()


def today_str() -> str:
    return today().isoformat()
