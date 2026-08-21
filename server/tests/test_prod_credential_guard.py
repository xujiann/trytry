"""生产凭据强度守卫（🔴 P0 安全止血）。

修的洞：原守卫只比对"是否**等于**代码里的默认值"，只挡得住一个字符串。
实测两处漏网——

1. `docker-compose.yml` 注入的是 `change-me-in-production`，而代码默认是
   `dev-secret-change-in-production`，两者不相等；该文件里又写着 `MEDPLAT_ENV: prod`，
   于是 `docker compose up` 不设任何环境变量也能起来——起来的是一个**用仓库里
   人人可见的字符串签 JWT** 的生产实例，谁都能离线伪造管理员令牌。
2. `MEDPLAT_SECRET=x` 这种一字符密钥同样一路放行。

现改为长度 + 字符多样性 + 占位符词表三重校验，并把部署文件里的弱默认值一并去掉。
"""
import re
from pathlib import Path

import pytest

from app.config import (
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_SECRET,
    MIN_ADMIN_PASSWORD_LENGTH,
    MIN_SECRET_LENGTH,
    Settings,
)

ROOT = Path(__file__).resolve().parent.parent.parent
STRONG_SECRET = "9f3c2b7e" * 4 + "a1d4"          # 36 位、字符够杂
STRONG_PASSWORD = "Kx7!mQ2$vLp9"                  # 12 位、含多类字符


def _prod(**kwargs) -> Settings:
    # database_url 给合规的 PG 串：A2 起"生产 + SQLite"另有守卫拒启
    # （见 test_ops_prod_guard.py），本文件只聚焦凭据强度这一层。
    base = {
        "env": "prod",
        "secret": STRONG_SECRET,
        "admin_password": STRONG_PASSWORD,
        "database_url": "postgresql://medplat:pw@db:5432/medplat",
    }
    return Settings(**{**base, **kwargs})


def test_强凭据可以启动():
    assert _prod().is_production


def test_非生产不校验强度():
    """本地开发就该能直接跑起来——守卫只在生产收紧。"""
    s = Settings(env="dev", secret=DEFAULT_SECRET, admin_password=DEFAULT_ADMIN_PASSWORD)
    assert not s.is_production


@pytest.mark.parametrize(
    "secret,reason",
    [
        (DEFAULT_SECRET, "代码里的默认值"),
        ("change-me-in-production", "docker-compose 曾注入的占位符（原守卫放行）"),
        ("x", "一字符（原守卫放行）"),
        ("a" * 40, "长度够但只有一种字符，熵为零"),
        ("your-secret-key-goes-right-here-ok", "含 your-secret 字样"),
        ("PLACEHOLDER-PLACEHOLDER-PLACEHOLDER", "含 placeholder 字样"),
    ],
)
def test_弱密钥一律拒绝启动(secret, reason):
    with pytest.raises(Exception) as exc:
        _prod(secret=secret)
    assert "MEDPLAT_SECRET" in str(exc.value), reason


@pytest.mark.parametrize(
    "password",
    [
        DEFAULT_ADMIN_PASSWORD,
        "admin123!",
        "a",
        "b" * 20,
        "password1234",
        "123456789012",   # 长度够、不同字符也够，但只有一类：纯数字
        "abcdefghijklm",  # 同理：纯小写字母
    ],
)
def test_弱管理员口令一律拒绝启动(password):
    with pytest.raises(Exception) as exc:
        _prod(admin_password=password)
    assert "MEDPLAT_ADMIN_PASSWORD" in str(exc.value)


def test_刚好达标与差一位():
    """边界钉住：长度判据是 `< min` 拒绝，等于 min 放行。"""
    # 恰好 32 位、8 种不同字符、含小写+数字两类——三项判据都刚好压线通过
    ok = "abcdefg1" * (MIN_SECRET_LENGTH // 8)
    assert len(ok) == MIN_SECRET_LENGTH
    _prod(secret=ok)                       # 刚好 32 位、8 种字符 → 放行
    with pytest.raises(Exception):
        _prod(secret=ok[:-1])              # 差一位 → 拒绝

    pwd = "Ab1!Cd2@Ef3#"
    assert len(pwd) == MIN_ADMIN_PASSWORD_LENGTH
    _prod(admin_password=pwd)
    with pytest.raises(Exception):
        _prod(admin_password=pwd[:-1])


def test_两个都弱时把问题一次报全():
    """一次列清，别让运维改一个重启一次、再撞下一个。"""
    with pytest.raises(Exception) as exc:
        _prod(secret="x", admin_password="y")
    message = str(exc.value)
    assert "MEDPLAT_SECRET" in message and "MEDPLAT_ADMIN_PASSWORD" in message


# ---------------------------------------------------------------- 部署文件


def test_部署文件里不得留弱凭据默认值():
    """守卫是最后一道；部署样例本身就不该写出一个能跑起来的弱值。

    `${VAR:-默认}` 形态尤其危险：不设环境变量也能起来，而"能起来"就意味着
    有人会那样跑。改用 `${VAR:?提示}`，没设就报错退出。
    """
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    # 注释里允许提到这些字样（解释历史），只检查真正的赋值行
    assignments = [
        line for line in compose.splitlines()
        if not line.lstrip().startswith("#") and ":" in line
    ]
    body = "\n".join(assignments)
    for weak in (DEFAULT_ADMIN_PASSWORD, "change-me-in-production"):
        assert weak not in body, f"docker-compose.yml 的赋值里仍有弱默认值 {weak!r}"
    for var in ("MEDPLAT_SECRET", "MEDPLAT_ADMIN_PASSWORD"):
        assert not re.search(rf"\$\{{{var}:-", body), (
            f"{var} 用了 `:-默认值` 形态——不设也能起来。应改为 `:?提示` 强制显式提供"
        )
        assert re.search(rf"\$\{{{var}:\?", body), f"{var} 应为 `${{{var}:?提示}}` 形态"


def test_render演示站不用代码默认密钥():
    """演示站也不该用仓库里人人可见的密钥签 JWT。"""
    render = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "MEDPLAT_SECRET" in render, "render.yaml 未设置 MEDPLAT_SECRET，会退回代码默认值"
    assert "generateValue: true" in render, "应让平台生成随机值，而不是写死在文件里"
