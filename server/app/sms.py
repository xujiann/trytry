"""短信通道适配层：验证码下发的可插拔驱动。

驱动由 MEDPLAT_SMS_PROVIDER 选择：

- ``console``（默认）：不外发，验证码只打日志；且**非生产环境**下发接口会在
  响应里回显 ``debug_code``，便于本地联调与演示站直接登录。生产环境即使配
  console 也不回显（见 routers/portal.py），只是短信发不出去而已。
- ``http``：把短信投递给自建/云短信网关（POST JSON 到 MEDPLAT_SMS_GATEWAY_URL）。
  阿里云/腾讯云等各家签名算法不同，此处走"统一网关"这层薄封装，接入方按自家
  网关协议实现一次即可，不把厂商 SDK 拖进本仓库。

新增厂商直连时实现 SmsProvider 协议并在 _build_provider 注册即可。
"""
import logging
from typing import Protocol

from .config import settings

logger = logging.getLogger("medplat.sms")


class SmsProvider(Protocol):
    name: str

    def send(self, phone: str, content: str) -> bool:
        """投递短信；成功返回 True。实现方不得抛异常，失败一律返回 False。"""
        ...


class ConsoleSmsProvider:
    """开发/演示通道：只写日志，不产生外部调用。"""

    name = "console"

    def send(self, phone: str, content: str) -> bool:
        logger.info("[SMS-CONSOLE] to=%s content=%s", phone, content)
        return True


class HttpGatewaySmsProvider:
    """通用 HTTP 短信网关通道。

    请求体 ``{"phone": ..., "content": ..., "sign": ...}``，网关返回 2xx 视为受理。
    网络异常/超时按投递失败处理，由调用方决定是否提示重试——绝不因为通道抖动
    把已生成的验证码当成"已发出"。
    """

    name = "http"

    def __init__(self, url: str, api_key: str, sign_name: str) -> None:
        self.url = url
        self.api_key = api_key
        self.sign_name = sign_name

    def send(self, phone: str, content: str) -> bool:
        if not self.url:
            logger.error("[SMS-HTTP] 未配置 MEDPLAT_SMS_GATEWAY_URL，短信未发送")
            return False
        import httpx

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            resp = httpx.post(
                self.url,
                json={"phone": phone, "content": content, "sign": self.sign_name},
                headers=headers,
                timeout=5.0,
            )
        except Exception:  # pragma: no cover - 依赖真实网络
            logger.exception("[SMS-HTTP] 短信网关调用异常 phone=%s", phone)
            return False
        if resp.status_code >= 400:  # pragma: no cover - 依赖真实网络
            logger.error("[SMS-HTTP] 网关拒绝 status=%s body=%s", resp.status_code, resp.text[:200])
            return False
        return True  # pragma: no cover - 依赖真实网络


def _build_provider() -> SmsProvider:
    if settings.sms_provider == "http":
        return HttpGatewaySmsProvider(settings.sms_gateway_url, settings.sms_api_key, settings.sms_sign_name)
    return ConsoleSmsProvider()


_provider: SmsProvider | None = None


def get_sms_provider() -> SmsProvider:
    """进程内单例；测试可用 set_sms_provider 注入桩件。"""
    global _provider
    if _provider is None:
        _provider = _build_provider()
    return _provider


def set_sms_provider(provider: SmsProvider | None) -> None:
    """测试辅助：注入/复位通道实现。"""
    global _provider
    _provider = provider
