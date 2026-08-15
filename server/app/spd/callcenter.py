"""呼叫通道：把呼叫任务真的派出去，而不是只记台账。

与平台的 `SmsProvider` / `PaymentGateway` 同一形状——协议 + 进程内单例 + 测试可注入。
两个内置实现：

- `manual`（默认）：不产生外部调用。呼叫任务建出来后由随访人员人工外呼，
  再经 `POST /api/spd/call-tasks/{id}/result` 回填结果——这是没有呼叫中心的县的
  真实工作方式，不是降级。
- `http`：POST 到呼叫中心网关（`MEDPLAT_SPD_CALL_GATEWAY_URL`），请求体
  `{"task_id", "phone", "ref_type"}`；网关回 2xx 视为受理，呼叫结果由网关回调
  同一个 result 接口（含录音地址）。

派发失败不抛异常：任务留在 `pending`，`result` 里记失败原因——
呼叫中心抖一下不该让"发起随访"这个动作失败。
"""
from __future__ import annotations

import logging
from typing import Protocol

from ..config import settings

logger = logging.getLogger("medplat.spd.callcenter")


class CallProvider(Protocol):
    name: str

    def dispatch(self, task_id: int, phone: str, ref_type: str) -> tuple[bool, str]:
        """派发一通呼叫；返回 (是否受理, 说明)。实现不得抛异常。"""
        ...


class ManualCallProvider:
    """人工外呼：派发即受理（等着人打电话），不产生外部调用。"""

    name = "manual"

    def dispatch(self, task_id: int, phone: str, ref_type: str) -> tuple[bool, str]:
        return True, "待人工外呼"


class HttpCallProvider:
    name = "http"

    def __init__(self, url: str):
        self.url = url

    def dispatch(self, task_id: int, phone: str, ref_type: str) -> tuple[bool, str]:
        if not self.url:
            return False, "未配置 MEDPLAT_SPD_CALL_GATEWAY_URL"
        import httpx

        try:
            resp = httpx.post(
                self.url,
                json={"task_id": task_id, "phone": phone, "ref_type": ref_type},
                timeout=5,
            )
        except Exception as exc:  # noqa: BLE001 - 通道故障不外抛，见模块文档
            logger.warning("呼叫网关派发失败：%s", exc)
            return False, f"呼叫网关异常：{exc}"[:200]
        if resp.status_code // 100 == 2:
            return True, "已派发至呼叫中心"
        return False, f"呼叫网关返回 {resp.status_code}"


_provider: CallProvider | None = None


def get_call_provider() -> CallProvider:
    global _provider
    if _provider is None:
        if settings.spd_call_provider == "http":
            _provider = HttpCallProvider(settings.spd_call_gateway_url)
        else:
            _provider = ManualCallProvider()
    return _provider


def set_call_provider(provider: CallProvider | None) -> None:
    """测试注入口；传 None 恢复按配置重建。"""
    global _provider
    _provider = provider
