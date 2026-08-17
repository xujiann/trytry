"""应用内领域事件总线。

平台**发布**事件，子系统**订阅**事件——依赖方向因此保持单向：
平台不知道谁在听（它只 import 本模块），子系统也不必去扫平台的表。

## 为什么要有它

慢专病的随访方案匹配原先是"回溯 N 天扫出院记录"（`followup-plans/auto-match`）。
扫表有两个改不掉的毛病：

1. **有延迟**：出院到随访计划生成之间隔着一个扫描周期；
2. **要么漏要么重**：窗口开小了漏，开大了重复扫，而"重复"要靠业务侧再做一次
   幂等判断——判断写在扫描里，扫描没跑就什么都没有。

事件把触发点挪到**动作发生的那一刻**，扫描退化成兜底而不是主路径。

## 契约（三条，都是从平台既有的 `notify.py` 学来的）

1. **同事务、只 add 不 commit**：订阅者拿到的是发布方的 `Session`，写入随发布方
   的事务一起提交或一起回滚。业务回滚了、随访计划却建出来了，是最难查的那种脏数据。
2. **订阅者不得抛异常**：一个订阅者炸掉不能连累业务主流程。本模块统一兜住并记日志——
   出院登记不该因为"随访模块的一个空指针"而失败。
3. **订阅者必须幂等**：同一事件可能因重试而重复投递，订阅者要自己判重。

## 刻意不做的事

不做异步队列、不做跨进程投递。平台已经有 ESB（`/api/esb`）负责跨系统消息，
这里只解决**同一个应用内**的模块解耦；再造一个队列只会多一处要运维的东西。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable

from sqlalchemy.orm import Session

logger = logging.getLogger("medplat.events")

#: 事件名 → 订阅者列表。订阅在模块导入时完成（见 app/spd/subscribers.py）。
_subscribers: dict[str, list[Callable[[Session, dict], None]]] = defaultdict(list)

# ---------------------------------------------------------------- 事件名清单
#
# 集中定义而不是各处写字符串：拼错的事件名不会报错，只会**静默地没人听**，
# 而"没人听"和"逻辑正常但没命中"在日志里长得一模一样。

ENCOUNTER_CREATED = "encounter.created"        # 门急诊/住院就诊登记
ADMISSION_DISCHARGED = "admission.discharged"  # 出院办理完成

KNOWN_EVENTS = (ENCOUNTER_CREATED, ADMISSION_DISCHARGED)


def subscribe(event: str, handler: Callable[[Session, dict], None]) -> None:
    """注册订阅者。未知事件名直接拒绝——见上面为什么。"""
    if event not in KNOWN_EVENTS:
        raise ValueError(f"未知事件名：{event}；请先加进 events.KNOWN_EVENTS")
    _subscribers[event].append(handler)


def publish(db: Session, event: str, payload: dict) -> int:
    """发布事件，返回被调用的订阅者数量。

    调用方随后照常 `db.commit()`；订阅者写入的数据一并提交。
    """
    handlers = _subscribers.get(event, [])
    for handler in handlers:
        try:
            handler(db, payload)
        except Exception:  # noqa: BLE001 - 见模块文档契约 2
            logger.exception("领域事件订阅者异常：%s → %s", event, getattr(handler, "__name__", handler))
    return len(handlers)


def subscriber_count(event: str) -> int:
    """供自检与用例使用：某个事件当前有几个订阅者。"""
    return len(_subscribers.get(event, []))
