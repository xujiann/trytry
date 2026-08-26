"""割接前配置体检脚本的守卫（上线前审计 A5）。

**这个脚本存在的理由**：生产守卫已经对「弱密钥/SQLite/演示种子/多实例无 Redis/
通道枚举写错」拒绝启动，但还有十几项是「缺失时不拒启」的——不配杀毒地址附件
就不扫、不配日志文件等保留存就不满足、不配告警出口任务失败就没人知道。
它们各自都可能有正当的「本县暂不需要」，所以不能一刀切拒启；也正因为不拒启，
它们最容易漏，而仓库里此前**没有任何一份必设清单**。

**这个测试存在的理由**：清单类的东西最容易悄悄失效——字段改名了、新加了危险
默认值、或者有人把「查不到什么」那段诚实说明删掉，脚本照样退出 0。
下面几条钉的就是这些失效方式，而不是逐字复读清单内容。
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent
SCRIPT = SERVER / "scripts" / "preflight_check.py"

STRONG_SECRET = "9f3c2b7e" * 4 + "a1d4"
STRONG_PASSWORD = "Kx7!mQ2$vLp9"
PG_URL = "postgresql://medplat:pw@db:5432/medplat"

#: 把所有高危项都配上真值——用来验「配齐了就该放行」。
ALL_CONFIGURED = {
    "MEDPLAT_SMS_PROVIDER": "http",
    "MEDPLAT_WECHAT_PROVIDER": "official",
    "MEDPLAT_WECHAT_APPID": "wx_test",
    "MEDPLAT_PII_ENCRYPTION_ENABLED": "true",
    "MEDPLAT_LOG_FILE": "/var/log/medplat.log",
    "MEDPLAT_CLAMD_ADDRESS": "127.0.0.1:3310",
    "MEDPLAT_AUDIT_ANCHOR_WEBHOOK_URL": "https://anchor.example.com/h",
    "MEDPLAT_ALERT_WEBHOOK_URL": "https://alert.example.com/h",
    "MEDPLAT_TOTP_REQUIRED_ROLES": "admin,director",
    "MEDPLAT_DB_POOL_TIMEOUT_SECONDS": "10",
}


def _run(**env) -> subprocess.CompletedProcess:
    """在干净的环境里跑脚本——宿主残留会让红绿判定失真。"""
    clean = {
        k: v for k, v in os.environ.items()
        if not k.startswith("MEDPLAT_")
    }
    clean["PATH"] = os.environ.get("PATH", "")
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=SERVER, env={**clean, **env}, capture_output=True, text=True, timeout=120,
    )


def _prod_env(**extra) -> dict:
    return {
        "MEDPLAT_ENV": "prod",
        "MEDPLAT_SECRET": STRONG_SECRET,
        "MEDPLAT_ADMIN_PASSWORD": STRONG_PASSWORD,
        "MEDPLAT_DATABASE_URL": PG_URL,
        **extra,
    }


# ---------------------------------------------------------------- 退出码语义


def test_非生产环境返回2并明说这份报告没有意义():
    """在开发机上跑查的是本机的值。不提醒的话，人会拿它当割接结论。"""
    r = _run(MEDPLAT_ENV="dev")
    assert r.returncode == 2, r.stdout
    assert "不是生产环境" in r.stdout
    assert "没有意义" in r.stdout


def test_生产全默认值返回1并列出未决项():
    r = _run(**_prod_env())
    assert r.returncode == 1, r.stdout
    assert "项高危未决" in r.stdout
    # 抽查几项：这些取默认值就是「功能静默不工作」
    for name in ("sms_provider", "log_file", "clamd_address"):
        assert name in r.stdout


def test_高危项全部配齐后返回0():
    """防「怎么配都过不了」——一个永远红的门会被人绕过去，等于没有。"""
    r = _run(**_prod_env(**ALL_CONFIGURED))
    assert r.returncode == 0, r.stdout
    assert "高危项均已配置或已登记" in r.stdout


def test_登记机制能让本县不需要的项不再报警():
    """不给登记入口，清单就会因为常年有几项报红而被整体忽略。"""
    r = _run(**_prod_env(MEDPLAT_PREFLIGHT_ACK="clamd_address,totp_required_roles"))
    assert "clamd_address" in r.stdout and "已登记" in r.stdout
    # 登记不是"全过"——其余高危项照样要拍板
    assert r.returncode == 1, r.stdout


def test_配了反而危险的项会被单独点名():
    """`portal_legacy_verify` 是免登录的证件号查询面，默认关是整改结论。"""
    r = _run(**_prod_env(**ALL_CONFIGURED, MEDPLAT_PORTAL_LEGACY_VERIFY="true"))
    assert r.returncode == 1, r.stdout
    assert "portal_legacy_verify" in r.stdout
    assert "配了反而危险" in r.stdout


# ---------------------------------------------------------------- 防清单失效


def _load_module():
    sys.path.insert(0, str(SERVER / "scripts"))
    try:
        import importlib

        return importlib.import_module("preflight_check")
    finally:
        sys.path.pop(0)


def test_清单里的每个配置名都必须真的存在于Settings():
    """字段改名/敲错时，这一项会**静默地永远不报警**——清单类东西的头号失效方式。

    不是假想：`Settings` 是 pydantic 模型，改名不会有任何编译期提示，
    而 preflight 只在割接那天跑一次，没人会注意到少了一项。
    """
    from app.config import Settings

    mod = _load_module()
    fields = set(Settings.model_fields)
    checked = [name for name, *_ in mod.CHECKS] + [name for name, *_ in mod.DANGEROUS_IF_SET]
    unknown = [n for n in checked if n not in fields]
    assert unknown == [], f"清单里这些名字不是 Settings 的字段，永远不会报警：{unknown}"


def test_判定函数在默认配置下真的会命中():
    """防空转：判定写反（比如把 `not x` 写成 `x`）会让高危项恒绿。

    用一个全默认的 Settings 过一遍——这些项的默认值**就是**危险值，
    所以每一条都必须命中。一条都不命中就说明判定失效了。
    """
    from app.config import Settings

    mod = _load_module()
    default = Settings(env="dev")
    hit = [name for name, _lvl, is_default, *_ in mod.CHECKS if is_default(default)]
    assert len(hit) == len(mod.CHECKS), (
        f"这些项在全默认配置下没有命中，判定可能写反了："
        f"{[n for n, *_ in mod.CHECKS if n not in hit]}"
    )


def test_必须如实列出查不到的东西():
    """一个体检脚本最坏的失败方式，是让人以为"跑过了就没问题"。

    线 A 那几件（备份能不能真恢复、归档延迟几秒、通道能不能联通、国产库跑不跑得起来）
    都不在这个脚本的能力范围内，删掉这段说明比留着更危险。
    """
    mod = _load_module()
    assert len(mod.NOT_COVERED) >= 4
    joined = "".join(mod.NOT_COVERED)
    for keyword in ("备份", "归档延迟", "通道", "可达"):
        assert keyword in joined, f"「查不到」清单里缺少 {keyword} 这一类"


@pytest.mark.parametrize("level_attr", ["HIGH", "ADVISORY"])
def test_两个档次都真的有项目(level_attr):
    """全挤在一个档次说明分级没起作用——要么天天报红被忽略，要么什么都不拦。"""
    mod = _load_module()
    level = getattr(mod, level_attr)
    assert any(lvl == level for _n, lvl, *_ in mod.CHECKS), f"{level_attr} 档一项都没有"


def test_配置本身非法时给人话而不是一段栈():
    """自己的测试逼出来的：弱密钥会让 Settings 在 import 期就抛，stdout 全空。

    "配置校验就没过"意味着这套配置连启动都启动不了，比任何一项高危未决更早，
    正是最该被清楚说出来的结果——割接现场看到一段 traceback 没有用。
    """
    r = _run(MEDPLAT_ENV="prod", MEDPLAT_SECRET="aaaa" * 8,
             MEDPLAT_ADMIN_PASSWORD=STRONG_PASSWORD, MEDPLAT_DATABASE_URL=PG_URL)
    assert r.returncode == 1, r.stdout
    assert "连启动都启动不了" in r.stdout, "配置非法时 stdout 应给出人话"
    assert "Traceback" not in r.stdout
