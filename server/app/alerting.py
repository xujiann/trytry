"""主动告警通道（工程包 P2）：运行事件外发 webhook。

平台此前的"告警"只有两条路：WebSocket 广播（无人在线即空操作）和错误日志
（没人盯着就等于没写）。任务失败、备份/归档异常这类**必须有人知道**的事件，
需要一条推得出去的通道。这里补上最小实现：POST 一条平台自定的 JSON
（`{service, kind, message, at}`）到运维配置的 webhook。

企业微信 / 钉钉群机器人要的是它们自己的消息格式（`{"msgtype": "text", "text": {"content": …}}`
一类），认不得这份报文，不能把群机器人地址直接配进来，中间要有告警平台或一层转换（P2-1268；
原先这里写「群机器人、告警平台皆可承接」，照着配了群里一条也收不到）。要不要内置群机器人格式待裁定。

约定：

- `MEDPLAT_ALERT_WEBHOOK_URL` 为空即整体关闭——send_alert 是空操作，零外呼；
- 对端回 2xx 且应答体不是带非 0 `errcode` 的 JSON 才算送达（P2-266 / P2-1268）；
- 同 kind 按 `MEDPLAT_ALERT_COOLDOWN_SECONDS`（默认 600s）冷却，防故障风暴
  刷爆接收群；发送**失败也占冷却**——网关宕机时高频失败源每次都外呼一遍，
  5 秒超时 × 每次失败会把调度循环拖慢，宁可少发不可拖垮主流程；
- 发送失败仅记日志**绝不抛**：告警是旁路，不能反过来打断业务/调度。

冷却状态沿用 state_store 的分层取舍，但**只做进程内实现**（dict + 锁）：
多实例部署下各实例独立冷却，同一事件最多收到 N 份（N=实例数）。告警通道
宁可重复不可漏报，为去重引入 Redis 依赖不值当——集中去重交给接收端
（群机器人/告警平台多自带按内容合并）。
"""
import logging
import threading
import time

import httpx

from .config import settings

logger = logging.getLogger("medplat.alerting")

#: webhook 请求超时（秒）：告警是旁路调用，宁可发不出去也不能长时间阻塞调用方
WEBHOOK_TIMEOUT_SECONDS = 5.0

_cooldown_lock = threading.Lock()
#: kind -> 最近一次尝试外发的单调钟时刻
_last_sent: dict[str, float] = {}


def send_alert(kind: str, message: str) -> bool:
    """外发一条运行告警；返回是否真的发出去了（对端收下）。

    - 未配置 webhook：直接返回 False（关闭态，零外呼）；
    - 同 kind 冷却期内：返回 False（已在冷却，不重复轰炸）；
    - 外呼失败（网络异常、对端回 4xx / 5xx、回 2xx 但应答 JSON 的 `errcode` 非 0）：记错误日志后返回 False，
      **不抛异常**。
    """
    url = settings.alert_webhook_url
    if not url:
        return False
    now = time.monotonic()
    with _cooldown_lock:
        last = _last_sent.get(kind)
        if last is not None and now - last < settings.alert_cooldown_seconds:
            return False
        _last_sent[kind] = now
    payload = {
        "service": "medplat",
        "kind": kind,
        "message": message,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    try:
        resp = httpx.post(url, json=payload, timeout=WEBHOOK_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - 告警外呼失败绝不打断调用方
        logger.error("告警 webhook 外呼失败（kind=%s），本条告警丢失", kind, exc_info=True)
        return False
    if not resp.is_success:
        # 对端回了 4xx / 5xx 就是没收下（P2-266）：原先只认网络异常，webhook 回 500 也返回「已发出」
        logger.error("告警 webhook 拒收（kind=%s，HTTP %s），本条告警丢失", kind, resp.status_code)
        return False
    errcode, errmsg = _rejected_errcode(resp)
    if errcode is not None:
        # 回 2xx 也可能没收下（P2-1268）：群机器人一类的接口格式不对时照回 HTTP 200，只在应答体的 errcode 里说
        # 拒收——原先只看状态码，记成已发出、一行错误也没有。日志带 errcode / errmsg，不带告警正文
        logger.error("告警 webhook 拒收（kind=%s，HTTP %s，errcode=%s，errmsg=%s），本条告警丢失",
                     kind, resp.status_code, errcode, errmsg)
        return False
    return True


def _rejected_errcode(resp: httpx.Response) -> tuple[object, str]:
    """应答体是带非 0 `errcode` 的 JSON 对象时返回 (errcode, errmsg)，否则 (None, "")（P2-1268）。

    errcode 为 0 / "0" / 空，应答体不是 JSON、是空体、是不带 errcode 的 JSON，照旧按 HTTP 状态码算送达。解析出什么
    错都按「不是 JSON」处理（告警是旁路，绝不抛）。errmsg 是对端写的，截到 200 字；告警正文不进日志。
    """
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - 非 JSON / 空体 / 畸形应答都不算对端拒收，也不能让旁路抛出去
        return None, ""
    if not isinstance(body, dict):
        return None, ""
    errcode = body.get("errcode")
    if errcode is None or errcode == 0 or str(errcode).strip() in ("", "0"):
        return None, ""
    return errcode, str(body.get("errmsg") or "")[:200]


def reset_cooldowns() -> None:
    """测试辅助：清空冷却状态。"""
    with _cooldown_lock:
        _last_sent.clear()
