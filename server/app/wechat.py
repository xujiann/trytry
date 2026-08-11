"""微信登录适配层：公众号网页授权（OAuth2）的可插拔驱动。

驱动由 MEDPLAT_WECHAT_PROVIDER 选择：

- ``mock``（默认）：本地联调/演示桩。授权链接直接指向站内回调，换取的 openid
  由 code 推导，全程不出网，因此单元测试与无公众号的演示站都能跑通完整流程。
- ``official``：微信公众平台网页授权。授权页 → code → ``sns/oauth2/access_token``
  换 openid，scope=snsapi_userinfo 时再取一次昵称。需配置 appid/secret/回调域名。

两者返回同一结构 ``{"openid", "unionid", "nickname"}``，上层 portal 路由不感知差异。
"""
import logging
from typing import Protocol
from urllib.parse import quote

from .config import settings

logger = logging.getLogger("medplat.wechat")

AUTHORIZE_ENDPOINT = "https://open.weixin.qq.com/connect/oauth2/authorize"
TOKEN_ENDPOINT = "https://api.weixin.qq.com/sns/oauth2/access_token"
USERINFO_ENDPOINT = "https://api.weixin.qq.com/sns/userinfo"


class WeChatProvider(Protocol):
    name: str

    def authorize_url(self, state: str) -> str:
        """生成微信授权页地址。"""
        ...

    def exchange_code(self, code: str) -> dict | None:
        """用授权 code 换取用户标识；失败返回 None（不抛异常）。"""
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


class OfficialWeChatProvider:
    """微信公众平台网页授权。"""

    name = "official"

    def __init__(self, appid: str, secret: str, redirect_uri: str) -> None:
        self.appid = appid
        self.secret = secret
        self.redirect_uri = redirect_uri

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
