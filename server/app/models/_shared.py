"""核心数据模型：对应规划第一期"平台支撑层 + 数据中心层"基础实体。

- User            统一认证用户（医共体工作人员账号）
- Organization    医共体成员单位（牵头医院/乡镇卫生院/村卫生室等）
- Patient         患者主索引（EMPI，电子健康卡号为对外统一标识）
- CodeSystem/CodeEntry  统一编码字典（诊断、药品、耗材、收费"四统一"）
- Referral        双向转诊记录（上转/下转，状态流转）
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    Numeric,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..clock import now_naive
from ..database import Base

#: 金额列类型（阶段十二）。
#:
#: 原先 43 个金额列用 `Float`：SQLite 存 REAL、PostgreSQL 存 double precision，
#: 库侧 SUM 会累积浮点误差——阶段七的 `test_fund.py` 里不得不写
#: `abs(distributed - 200000) < 1.0` 的容差断言才能通过。国产库（达梦/人大金仓）
#: 对浮点的处理与 SQLite 又不相同，信创适配时这批列是首当其冲的。
#:
#: 改为 `Numeric(14, 2)`：DDL 上是定点数，PostgreSQL 与国产库的 SUM/AVG 都是精确的，
#: 14 位精度对县域金额量级（百亿以下，精确到分）绰绰有余。
#:
#: **`asdecimal=False` 是一个明确的取舍**：Python 侧仍收发 float，不返回 Decimal。
#: 打开它会让全平台上百处 `Decimal * float` 的算式直接 TypeError，
#: 而那需要连同每一处金额计算一起改。所以这一步拿到的是**存储与库侧聚合的精确性**，
#: Python 侧的逐笔运算仍是浮点——端到端 Decimal 留作后续，理由与代价记在
#: `docs/信创适配与部署.md`，不含糊带过。
Money = Numeric(14, 2, asdecimal=False)


def utcnow() -> datetime:
    """所有 `created_at` 类列的默认值。

    P0-2：这里原先返回 aware（`datetime.now(timezone.utc)`），而调度器与居民端
    各自维护的 naive 版本写进的是同一批 `DateTime` 列——同一张表的两列可能一列
    aware 一列 naive。阶段二 `/api/monitor/overview` 拿 aware 值去比调度器写的
    naive `next_run_at`，直接 TypeError。现统一委托给 `clock.now_naive()`：
    **落库一律 naive UTC**。

    名字保留是因为它出现在近两百处列定义的 `default=` 里，改名的收益抵不上
    改动面；语义已经统一，新代码请直接用 `clock.now_naive()`。
    """
    return now_naive()


