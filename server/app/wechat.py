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
        if not code.startswith("mock-"):
            return None
        seed = code[len("mock-") :] or "demo"
        return {"openid": f"mock_{seed}", "unionid": "", "nickname": f"微信用户{seed[:6]}"}

    def send_template_message(self, openid: str, template_id: str, data: dict, url: str = "") -> bool:
        """联调桩：不出网，只把要发的内容落日志。"""
        logger.info(
            "[WECHAT-MOCK] 模板消息 openid=%s template=%s url=%s data=%s",
            openid, template_id, url, data,
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
    if settings.wechat_provider == "official" and settings.wechat_appid:
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
