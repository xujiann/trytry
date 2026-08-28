"""微信登录适配层：公众号网页授权（OAuth2）的可插拔驱动。

驱动由 MEDPLAT_WECHAT_PROVIDER 选择：

- ``mock``（默认）：本地联调/演示桩。授权链接直接指向站内回调，换取的 openid
  由 code 推导，全程不出网，因此单元测试与无公众号的演示站都能跑通完整流程。
- ``official``：微信公众平台网页授权。授权页 → code → ``sns/oauth2/access_token``
  换 openid，scope=snsapi_userinfo 时再取一次昵称。需配置 appid/secret/回调域名。

两者返回同一结构 ``{"openid", "unionid", "nickname"}``，上层 portal 路由不感知差异。

工程包 I2 增补：模板消息触达（`send_template_message`）。official 模式经
公众号全局 access_token（client_credential，进程内缓存、过期自刷新）调
模板消息接口；mock 模式只落日志返回 True。模板 id 不进 config——由业务侧
从系统参数表（SystemParam，key = ``wechat_template_<category>``）取，见
app/notify.py 的旁路发送。
"""
import logging
import time
from typing import Protocol
from urllib.parse import quote

from .config import settings

logger = logging.getLogger("medplat.wechat")

AUTHORIZE_ENDPOINT = "https://open.weixin.qq.com/connect/oauth2/authorize"
TOKEN_ENDPOINT = "https://api.weixin.qq.com/sns/oauth2/access_token"
USERINFO_ENDPOINT = "https://api.weixin.qq.com/sns/userinfo"
# 模板消息：公众号全局凭据 + 模板消息发送接口（与网页授权的 sns 凭据是两套）
CLIENT_TOKEN_ENDPOINT = "https://api.weixin.qq.com/cgi-bin/token"
TEMPLATE_SEND_ENDPOINT = "https://api.weixin.qq.com/cgi-bin/message/template/send"


class WeChatProvider(Protocol):
    name: str

    def authorize_url(self, state: str) -> str:
        """生成微信授权页地址。"""
        ...

    def exchange_code(self, code: str) -> dict | None:
        """用授权 code 换取用户标识；失败返回 None（不抛异常）。"""
        ...

    def send_template_message(self, openid: str, template_id: str, data: dict, url: str = "") -> bool:
        """发一条模板消息；成功返回 True。实现不得抛异常，失败一律返回 False。

        ``data`` 传平铺的 {字段名: 值}，official 实现按微信要求包成
        {字段名: {"value": 值}}。
        """
        ...


class MockWeChatProvider:
    """本地联调桩：code 形如 ``mock-<seed>``，openid 由 seed 推导且稳定可重现。"""

    name = "mock"

    def authorize_url(self, state: str) -> str:
        return f"/m/?code=mock-{state}&state={state}"

    def mock_code(self, state: str) -> str:
        """桩件专用：让前端无需跳转即可完成一次"授权"。"""
        return f"mock-{state}"

    def exchange_code(self, code: str) -> dict | None:
        # 生产环境硬门：桩件只要 code 以 `mock-` 开头就发 openid，而
        # `POST /api/portal/auth/wechat/login` 是**公开**端点——生产上留着这条，
        # 等于任何人构造一个 `code=mock-x` 就能开一个居民账号。
        # 与 `sms_debug_echo` 同一口径（生产即便显式配了也永不回显）：
        # 配置能不能配错是一回事，**生产上这条路必须走不通**是另一回事。
        # 不拒启而是在使用处失败——只用现金、没上微信的县不该被拒绝启动。
        if settings.is_production:
            logger.error(
                "生产环境仍在使用微信 mock 桩，已拒绝换码。桩会让任何 "
                "`code=mock-xxx` 登录成功并开户；请配置 MEDPLAT_WECHAT_PROVIDER=official "
                "与 appid/secret，或确认本县不开放微信登录。"
            )
            return None
        if not code.startswith("mock-"):
            return None
        seed = code[len("mock-") :] or "demo"
        return {"openid": f"mock_{seed}", "unionid": "", "nickname": f"微信用户{seed[:6]}"}

    def send_template_message(self, openid: str, template_id: str, data: dict, url: str = "") -> bool:
        """联调桩：不出网，只把要发的**字段名**落日志。

        原实现打的是 `data=%s`——模板消息的 value 里是姓名、就诊时间这类居民信息，
        而 `medplat.*` 现在全部落 stdout + 轮转文件（等保 6 个月留存）。
        打字段名足够联调（看得出模板对不对、少没少字段），打值就是把居民信息
        写进留存档案。同 `sms.ConsoleSmsProvider` 的取舍。
        """
        logger.info(
            "[WECHAT-MOCK] 模板消息 openid=%s template=%s url=%s data字段=%s",
            openid, template_id, url, sorted(data),
        )
        return True


class OfficialWeChatProvider:
    """微信公众平台网页授权 + 模板消息。"""

    name = "official"

    def __init__(self, appid: str, secret: str, redirect_uri: str) -> None:
        self.appid = appid
        self.secret = secret
        self.redirect_uri = redirect_uri
        # 公众号全局 access_token 缓存（进程内单副本；多实例部署各自缓存，
        # 微信允许同 appid 并存多个有效 token，无须共享存储）
        self._access_token = ""
        self._token_expires_at = 0.0

    def authorize_url(self, state: str) -> str:
        return (
            f"{AUTHORIZE_ENDPOINT}?appid={self.appid}"
            f"&redirect_uri={quote(self.redirect_uri, safe='')}"
            f"&response_type=code&scope=snsapi_userinfo&state={quote(state, safe='')}"
            "#wechat_redirect"
        )

    def exchange_code(self, code: str) -> dict | None:  # pragma: no cover - 依赖微信服务
        import httpx

        try:
            resp = httpx.get(
                TOKEN_ENDPOINT,
                params={
                    "appid": self.appid,
                    "secret": self.secret,
                    "code": code,
                    "grant_type": "authorization_code",
                },
                timeout=5.0,
            )
            data = resp.json()
        except Exception:
            logger.exception("[WECHAT] 换取 access_token 异常")
            return None
        openid = data.get("openid")
        if not openid:
            logger.error("[WECHAT] 授权失败 errcode=%s errmsg=%s", data.get("errcode"), data.get("errmsg"))
            return None
        nickname = ""
        try:
            info = httpx.get(
                USERINFO_ENDPOINT,
                params={"access_token": data.get("access_token", ""), "openid": openid, "lang": "zh_CN"},
                timeout=5.0,
            ).json()
            nickname = info.get("nickname", "")
        except Exception:
            # 昵称拿不到不影响登录，openid 才是身份
            logger.warning("[WECHAT] 获取用户昵称失败，忽略")
        return {"openid": openid, "unionid": data.get("unionid", "") or "", "nickname": nickname}

    def _client_access_token(self, force_refresh: bool = False) -> str:
        """公众号全局 access_token（缓存；提前 60 秒视为过期）。失败返回空串。"""
        if not force_refresh and self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token
        import httpx

        try:
            data = httpx.get(
                CLIENT_TOKEN_ENDPOINT,
                params={"grant_type": "client_credential", "appid": self.appid, "secret": self.secret},
                timeout=5.0,
            ).json()
        except Exception:
            logger.exception("[WECHAT] 获取公众号 access_token 异常")
            return ""
        token = data.get("access_token", "")
        if not token:
            logger.error(
                "[WECHAT] 获取 access_token 失败 errcode=%s errmsg=%s",
                data.get("errcode"), data.get("errmsg"),
            )
            return ""
        self._access_token = token
        self._token_expires_at = time.time() + float(data.get("expires_in", 7200))
        return token

    def send_template_message(self, openid: str, template_id: str, data: dict, url: str = "") -> bool:
        """公众号模板消息。token 失效（40001/42001）时强刷重试一次。"""
        import httpx

        payload = {
            "touser": openid,
            "template_id": template_id,
            "url": url,
            "data": {k: {"value": str(v)} for k, v in data.items()},
        }
        force_refresh = False
        for _ in range(2):
            token = self._client_access_token(force_refresh)
            if not token:
                return False
            try:
                result = httpx.post(
                    TEMPLATE_SEND_ENDPOINT, params={"access_token": token}, json=payload, timeout=5.0
                ).json()
            except Exception:
                logger.exception("[WECHAT] 模板消息发送异常 openid=%s", openid)
                return False
            errcode = int(result.get("errcode", -1))
            if errcode == 0:
                return True
            if errcode in (40001, 42001) and not force_refresh:
                force_refresh = True  # 缓存 token 已被吊销/过期：强刷一次再试
                continue
            logger.error(
                "[WECHAT] 模板消息失败 openid=%s errcode=%s errmsg=%s",
                openid, errcode, result.get("errmsg"),
            )
            return False
        return False


def _build_provider() -> WeChatProvider:
    """按配置选驱动。**声明了 official 就绝不回落 Mock**，哪怕配置不全。

    原实现是 `provider == "official" and appid` —— 漏配 appid 就静默变成 Mock。
    那不是降级，是**认证绕过**：`MockWeChatProvider.exchange_code` 只要 code 以
    `mock-` 开头就发一个 openid（本文件 67-71 行），于是任何人构造
    `code=mock-随便什么` 都能登录并开户，唯一兜底只剩"未实名绑定看不到档案"。
    生产守卫（config.py）当时也不查通道 provider，所以这条路上没有任何一处会喊。

    口径与短信通道对齐——`sms.py` 的同一位置早就写对了，注释说得很清楚：
    置空网关地址而**不**回退 console，因为"console 会成功，等于把没发出去的
    验证码当成已发出"。同理，这里宁可返回一个**必定失败**的 official
    （空 appid 换码必然被微信拒绝），也不返回一个**会成功**的桩。
    失败是可见的，假成功不是。
    """
    if settings.wechat_provider == "official":
        if not settings.wechat_appid:
            logger.error(
                "MEDPLAT_WECHAT_PROVIDER=official 但未配置 MEDPLAT_WECHAT_APPID："
                "微信登录将一律失败。**不会**回落到 mock 桩——桩会让任何 "
                "`code=mock-xxx` 登录成功并开户。请补齐 appid/secret/回调域名，"
                "或显式改回 MEDPLAT_WECHAT_PROVIDER=mock。"
            )
        return OfficialWeChatProvider(
            settings.wechat_appid, settings.wechat_secret, settings.wechat_redirect_uri
        )
    return MockWeChatProvider()


_provider: WeChatProvider | None = None


def get_wechat_provider() -> WeChatProvider:
    global _provider
    if _provider is None:
        _provider = _build_provider()
    return _provider


def set_wechat_provider(provider: WeChatProvider | None) -> None:
    """测试辅助：注入/复位通道实现。"""
    global _provider
    _provider = provider
