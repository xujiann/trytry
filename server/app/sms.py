"""短信通道适配层：验证码下发的可插拔驱动。

驱动由 MEDPLAT_SMS_PROVIDER 选择：

- ``console``（默认）：不外发，验证码只打日志；仅当**显式开启**
  ``MEDPLAT_SMS_DEBUG_ECHO`` 且**非生产环境**时，下发接口才在响应里回显
  ``debug_code`` 便于本地联调（默认关闭，见 routers/portal.py）。生产环境
  即便误开开关也永不回显，只是短信发不出去而已。
- ``http``：把短信投递给自建/云短信网关（POST JSON 到 MEDPLAT_SMS_GATEWAY_URL）。
  阿里云/腾讯云等各家签名算法不同，此处走"统一网关"这层薄封装，接入方按自家
  网关协议实现一次即可，不把厂商 SDK 拖进本仓库。

新增厂商直连时实现 SmsProvider 协议并在 _assemble_provider 注册即可。
"""
import json
import logging
from typing import Protocol

from .config import settings
from .egress import UnresolvedHost, egress_url_rejection, signed_headers
from .privacy import mask_phone

logger = logging.getLogger("medplat.sms")


class SmsProvider(Protocol):
    name: str

    def send(self, phone: str, content: str) -> bool:
        """投递短信；成功返回 True。实现方不得抛异常，失败一律返回 False。"""
        ...


class ConsoleSmsProvider:
    """开发/演示通道：只写日志，不产生外部调用。

    **默认不打明文**：手机号走 `privacy.mask_phone`，短信正文整段隐去（只留字数）。
    正文里就是那串验证码——而验证码在库里是**只落散列**的
    （见 routers/portal.py「验证码只落散列」），把它明文写进日志等于绕开了那条设计。

    这条以前"看着没事"，是因为 `medplat.sms` 当时根本没有 handler、
    `logger.info` 在建记录之前就被丢掉了。日志改成全部 `medplat.*` 都落
    stdout + 轮转文件（等保 6 个月留存）之后，同一行代码就变成了
    **把手机号和一次性口令写进留存档案**——`docs/运维手册.md` 恰好还写着
    "访问日志不含请求体，不会落身份证号/电话等敏感字段"。

    明文只在**显式开关 + 非生产**下才打，复用居民端回显 `debug_code` 的那对条件
    （`MEDPLAT_SMS_DEBUG_ECHO` 默认关，生产恒不生效），不新增第 14 个开关。
    """

    name = "console"

    def send(self, phone: str, content: str) -> bool:
        if settings.sms_debug_echo and not settings.is_production:
            logger.info("[SMS-CONSOLE] to=%s content=%s", phone, content)
        else:
            logger.info(
                "[SMS-CONSOLE] to=%s content=<%d 字，已隐去；本地联调设 "
                "MEDPLAT_SMS_DEBUG_ECHO=1 可打明文>",
                mask_phone(phone),
                len(content),
            )
        return True


class HttpGatewaySmsProvider:
    """通用 HTTP 短信网关通道。

    请求体 ``{"phone": ..., "content": ..., "sign": ...}``，网关返回 2xx 视为受理。
    网络异常/超时按投递失败处理，由调用方决定是否提示重试——绝不因为通道抖动
    把已生成的验证码当成"已发出"。

    请求鉴权（I2，与支付网关同一口径，见 app/egress.py）：配置了
    MEDPLAT_SMS_API_KEY 时，除 ``Authorization: Bearer`` 外另带
    ``X-Timestamp`` / ``X-Signature``（HMAC-SHA256 对"时间戳.请求体原始字节"
    签名），自建网关据此验签+时间窗防重放；未配置 key 则维持裸 Bearer 兼容旧网关。

    厂商适配说明（部署期接入，不把厂商 SDK 拖进本仓库——运行时依赖只有 13 项）：

    - **阿里云短信**：自建薄网关里用官方 SDK（``alibabacloud_dysmsapi``）调
      ``SendSms``（AccessKey 签名、TemplateCode+TemplateParam 模板制），本类的
      ``content`` 对应模板变量、``sign_name`` 对应阿里云"短信签名"。
    - **腾讯云短信**：同法经 ``tencentcloud-sdk-python`` 调 ``SendSms``
      （SecretId/SecretKey TC3 签名、TemplateId+TemplateParamSet）。
    - 也可绕过统一网关直连：实现本文件的 SmsProvider 协议
      （``send(phone, content) -> bool``，实现内完成厂商签名与模板映射），
      在 ``_assemble_provider`` 里按新的 MEDPLAT_SMS_PROVIDER 取值注册。
    """

    name = "http"

    def __init__(self, url: str, api_key: str, sign_name: str) -> None:
        self.url = url
        self.api_key = api_key
        self.sign_name = sign_name

    def send(self, phone: str, content: str) -> bool:
        if not self.url:
            logger.error("[SMS-HTTP] 未配置 MEDPLAT_SMS_GATEWAY_URL（或未通过出网校验），短信未发送")
            return False
        import httpx

        body = json.dumps(
            {"phone": phone, "content": content, "sign": self.sign_name}, ensure_ascii=False
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers.update(signed_headers(self.api_key, body))  # HMAC 签名头，同支付口径
        try:
            resp = httpx.post(self.url, content=body, headers=headers, timeout=5.0)
        except Exception:  # pragma: no cover - 依赖真实网络
            # 号码打掩码：这条 ERROR 恰恰是最会被留存、被贴进工单的日志。
            logger.exception("[SMS-HTTP] 短信网关调用异常 phone=%s", mask_phone(phone))
            return False
        if resp.status_code >= 400:  # pragma: no cover - 依赖真实网络
            logger.error("[SMS-HTTP] 网关拒绝 status=%s body=%s", resp.status_code, resp.text[:200])
            return False
        return True  # pragma: no cover - 依赖真实网络


def _build_provider() -> SmsProvider:
    """按配置装配好的通道本身（不问能否缓存，见 _assemble_provider）。"""
    return _assemble_provider()[0]


def _assemble_provider() -> tuple[SmsProvider, bool]:
    """按配置装配通道；第二项说明这个结果能不能存进进程单例（P2-1221）。

    网关主机名这次没解析出来（`egress.UnresolvedHost`）时为 False：这一次照旧不予启用、置空网关地址，
    验证码照旧 502、日志照旧，只是不缓存。DNS 抖一下不等于地址配错——原先这个降级的通道照样存进单例、
    此后再不重建，这个 worker 的验证码登录、补绑、宣教短信一律 502 到重启，DNS 恢复了也不重查。
    解析到内网 / 环回 / 保留段、协议不对是配置本身的问题，重试也一样，降级即永久，照旧缓存。
    """
    if settings.sms_provider == "http":
        url = settings.sms_gateway_url
        settled = True
        problem = egress_url_rejection(url, "MEDPLAT_SMS_GATEWAY_URL") if url else None
        if problem is not None:
            # SSRF 防线（I2）：URL 指向内网/环回等非公网地址时拒绝启用通道。
            # 置空 url 而非回退 console——console 会"成功"，等于把没发出去的
            # 验证码当成已发出；置空后 send 一律失败并 log，语义诚实。
            url = ""
            settled = not isinstance(problem, UnresolvedHost)
        return HttpGatewaySmsProvider(url, settings.sms_api_key, settings.sms_sign_name), settled
    return ConsoleSmsProvider(), True


_provider: SmsProvider | None = None


def get_sms_provider() -> SmsProvider:
    """进程内单例；测试可用 set_sms_provider 注入桩件。

    只缓存定了的结论（P2-1221，见 _assemble_provider）：这次没解析出网关主机名而降级的通道只用这一次，
    下次调用重建、重新校验；校验通过的、被永久拒绝的照旧缓存，此后不再重复解析。
    """
    global _provider
    if _provider is not None:
        return _provider
    provider, settled = _assemble_provider()
    if settled:
        _provider = provider
    return provider


def set_sms_provider(provider: SmsProvider | None) -> None:
    """测试辅助：注入/复位通道实现。"""
    global _provider
    _provider = provider
