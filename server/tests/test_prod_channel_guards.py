"""外部通道的三道守卫：配置写错、生产仍用桩、生产"线上支付"落 Mock。

上线前审计查出来的一组缺陷，根因是同一个：**四条外部通道（支付/微信/短信/外呼）
选驱动的代码都写成「== 某个值 → 真实现，否则 → 兜底实现」，而生产守卫一条都不查。**
于是两件事同时成立：写错配置不会报错，只会静默退回桩；退回桩之后没有任何一处会喊。

其中最狠的一条是认证绕过——`MockWeChatProvider.exchange_code` 只要 code 以
`mock-` 开头就发 openid，而 `POST /api/portal/auth/wechat/login` 是**公开**端点。
生产上留着桩，等于任何人构造 `code=mock-x` 就能开一个居民账号。

三道守卫分工（刻意不都做成"拒启"）：

1. **取值校验**（所有环境）：枚举型配置写错即拒启。开发环境同样要查——
   本地把 provider 敲错、以为在测真通道其实一直在测桩，比生产事故更难发现。
2. **配置事故拒启**（生产）：`official` 却没有 appid。没有任何一种部署形态
   需要它，所以可以放心拒启。
3. **使用处硬门**（生产）：微信 mock 不换码、线上支付无网关不受理。
   这两条**刻意不做成拒启**——只用现金、没上微信的县不该被拒绝启动，
   但生产上这两条路必须走不通。口径抄的是 `sms_debug_echo`（生产即便显式
   配了也永不回显）与 `billing.py` 的 gateway 503（未配置绝不悄悄落回 Mock）。
"""
import logging
import secrets

import pytest

from app.config import Settings

STRONG_SECRET = secrets.token_hex(32)
STRONG_PASSWORD = "Kx7!mQ2$vLp9"
PG_URL = "postgresql://medplat:pw@db:5432/medplat"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """隔离宿主环境——照 test_ops_prod_guard.py 的先例，残留会让红绿判定失真。"""
    for name in (
        "MEDPLAT_SEED_DEMO", "MEDPLAT_REDIS_URL",
        "MEDPLAT_WORKERS", "MEDPLAT_MIGRATE_ON_START",
    ):
        monkeypatch.delenv(name, raising=False)


def _prod(**kwargs) -> Settings:
    base = {
        "env": "prod",
        "secret": STRONG_SECRET,
        "admin_password": STRONG_PASSWORD,
        "database_url": PG_URL,
    }
    return Settings(**{**base, **kwargs})


# ============================================================ 一、枚举取值校验


@pytest.mark.parametrize(
    "field,bad",
    [
        ("crypto_suite", "sm3"),        # 想写 sm，多打一个 3 → 静默退回通用算法
        ("sms_provider", "htpp"),       # 想写 http → 静默退回 console（只打日志）
        ("wechat_provider", "offical"), # 想写 official → 静默退回 mock（= 认证绕过）
        ("spd_call_provider", "auto"),  # 压根不存在的值 → 静默退回 manual
    ],
)
def test_枚举配置写错必须拒启而不是静默退回默认实现(field, bad):
    """拼写错误是配置里最常见的错，而这个形状把它变成了静默降级。

    四项共用一个缺陷形状（`sms.py` / `wechat.py` / `callcenter.py` /
    `gmcrypto.py` 选驱动处），所以一起收，不逐个打补丁。
    """
    with pytest.raises(RuntimeError, match="不是合法取值"):
        Settings(env="dev", **{field: bad})


@pytest.mark.parametrize(
    "field,good",
    [
        ("crypto_suite", "general"), ("crypto_suite", "sm"),
        ("sms_provider", "console"), ("sms_provider", "http"),
        ("wechat_provider", "mock"), ("wechat_provider", "official"),
        ("spd_call_provider", "manual"), ("spd_call_provider", "http"),
    ],
)
def test_合法取值必须放行(field, good):
    """防误伤：判据收得太紧会把正常部署拦在门外，那比不查更糟。"""
    assert getattr(Settings(env="dev", **{field: good}), field) == good


def test_取值校验在所有环境都生效而不只是生产():
    """开发环境写错 provider，会让人"以为在测真通道、其实一直在测桩"。

    这种"验错了对象"比生产事故更难发现——生产至少会有人报障。
    """
    with pytest.raises(RuntimeError, match="不是合法取值"):
        Settings(env="dev", sms_provider="htpp")
    with pytest.raises(RuntimeError, match="不是合法取值"):
        Settings(env="prod", secret=STRONG_SECRET, admin_password=STRONG_PASSWORD,
                 database_url=PG_URL, sms_provider="htpp")


# ============================================== 二、生产 official 缺 appid 拒启


def test_生产声明official却没有appid必须拒启():
    """这是无歧义的配置事故：声明了走公众号却不给 appid，登录必然全部失败。

    没有任何一种部署形态需要它，所以可以放心拒启（对比：只收现金的县没有
    支付网关是**合法**形态，那种就不能拒启）。
    """
    with pytest.raises(RuntimeError, match="MEDPLAT_WECHAT_APPID 为空"):
        _prod(wechat_provider="official", wechat_appid="")


def test_配齐appid后放行():
    assert _prod(wechat_provider="official", wechat_appid="wx1", wechat_secret="s").wechat_appid == "wx1"


def test_生产用mock不拒启():
    """刻意不拒启——不开放微信登录的县是合法形态。

    生产上的危险由使用处硬门堵（见下一节），不是靠拒绝启动。
    """
    assert _prod(wechat_provider="mock").wechat_provider == "mock"


# ================================================== 三、微信 mock 的生产使用处硬门


def _mock_provider():
    from app.wechat import MockWeChatProvider

    return MockWeChatProvider()


def test_非生产环境mock照常换码(monkeypatch):
    """先钉住"没坏"：演示站与本地联调依赖这条路，硬门不能顺手把它也堵了。"""
    from app import wechat

    monkeypatch.setattr(wechat.settings, "env", "dev")
    monkeypatch.setattr(wechat.settings, "environment", "dev")
    info = _mock_provider().exchange_code("mock-abc")
    assert info and info["openid"] == "mock_abc"


def test_生产环境mock一律不换码(monkeypatch, caplog):
    """认证绕过的正面回归：生产上 `code=mock-x` 必须换不出 openid。

    换得出就意味着任何人都能开一个居民账号——`/api/portal/auth/wechat/login`
    是公开端点，没有任何前置鉴权。
    """
    from app import wechat

    monkeypatch.setattr(wechat.settings, "env", "prod")
    with caplog.at_level(logging.ERROR, logger="medplat.wechat"):
        assert _mock_provider().exchange_code("mock-abc") is None
    assert "拒绝换码" in caplog.text, "堵住了但没留下任何痕迹，运维无从发现"


def test_生产环境下任何mock前缀都换不出openid(monkeypatch):
    """防"只堵了我测的那一个"：换几种 seed 都必须落空。"""
    from app import wechat

    monkeypatch.setattr(wechat.settings, "env", "prod")
    provider = _mock_provider()
    for code in ("mock-", "mock-1", "mock-" + "a" * 64, "mock-../../x"):
        assert provider.exchange_code(code) is None, f"{code} 仍能换出 openid"


def test_声明official就绝不回落mock(monkeypatch):
    """漏配 appid 时宁可返回一个必定失败的 official，也不返回一个会成功的桩。

    口径与 `sms.py` 一致：置空网关地址而不回退 console，因为"console 会成功，
    等于把没发出去的验证码当成已发出"。失败是可见的，假成功不是。
    """
    from app import wechat
    from app.wechat import MockWeChatProvider, OfficialWeChatProvider

    monkeypatch.setattr(wechat.settings, "wechat_provider", "official")
    monkeypatch.setattr(wechat.settings, "wechat_appid", "")
    provider = wechat._build_provider()
    assert isinstance(provider, OfficialWeChatProvider)
    assert not isinstance(provider, MockWeChatProvider)


def test_显式配mock时仍然给mock(monkeypatch):
    """防误伤：演示站与本地就是靠 mock 跑通完整流程的。"""
    from app import wechat
    from app.wechat import MockWeChatProvider

    monkeypatch.setattr(wechat.settings, "wechat_provider", "mock")
    assert isinstance(wechat._build_provider(), MockWeChatProvider)
