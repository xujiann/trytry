#!/usr/bin/env python3
"""割接前配置体检：一条命令列出所有「生产不该还是默认值」的项。

**为什么需要它。** 生产守卫（`app/config.py`）已经很硬——弱密钥、SQLite、
演示种子、多实例无 Redis、通道枚举值写错，这些都**拒绝启动**。但还有一批配置
是「缺失时不拒启」的：不配杀毒地址附件就不扫、不配日志文件等保留存就不满足、
不配告警出口任务失败就没人知道。它们各自都有正当的「本县暂不需要」的可能，
所以不能一刀切拒启；可正因为不拒启，**它们最容易漏**——环境变量表有几十行，
靠人眼对是对不出来的。

上线前审计实测：这类项有 13 个，而仓库里没有任何一份「必设清单」文档，
最接近的是运维手册里一张按字段罗列的参考表——它不带「必填」列，也没有汇总。

**这个脚本不替人做决定**，它只保证每一项都被**看见过一次**。所以它的输出不是
「对/错」而是「这一项现在是什么值、留着会怎样」，并且允许显式登记「本县确实
不需要」——见下面的 ACK 机制。

用法（在**目标生产环境的环境变量**下跑，否则查的是本机默认值）：

    cd server && python scripts/preflight_check.py

    # 本县确实不上杀毒与双因素，登记后不再报警（理由写进变更单，不是写进这里）
    MEDPLAT_PREFLIGHT_ACK=clamd_address,totp_required_roles \\
        python scripts/preflight_check.py

退出码：0=无未决高危项；1=有高危项未配置且未登记；2=环境不是生产（提示性）。

**它查不到的东西，如实列在输出末尾**——一个体检脚本最坏的失败方式是让人以为
「跑过了就没问题」。备份能不能恢复、归档延迟多少秒、国产库跑不跑得起来，
都不在这里，那些要在真实环境里做（见 docs/下一步开发计划.md 线 A）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_settings():
    """构造 Settings；**配置本身非法时给人话而不是一段栈**。

    这一条是自己的测试逼出来的：拿一个弱密钥跑本脚本，`from app.config import
    settings` 在 import 期就抛 RuntimeError，stdout 一个字都没有，割接现场只看到
    一段 traceback。而"配置校验就没过"恰恰是最该被清楚说出来的一种结果——
    它意味着这套配置**连启动都启动不了**，比任何一项高危未决都更早。
    """
    try:
        from app.config import settings

        return settings, None
    except Exception as exc:  # noqa: BLE001 —— 任何构造失败都要变成人话
        return None, str(exc)

#: 高危 = 取默认值即「功能静默不工作」或「合规/安全不达标」，割接前必须有人拍板。
HIGH = "高危"
#: 建议 = 取默认值仍能正常服务，但出事时会更难查/更难恢复。
ADVISORY = "建议"


def _pool_timeout_risk() -> str:
    return (
        "过载时请求无限排队，表现为整站假死而不是明确失败"
        "（app/database.py 自己写了这个后果）"
    )


#: (配置名, 档次, 判定函数 → True 表示「还是危险的默认值」, 风险说明, 建议)
CHECKS: list[tuple[str, str, object, str, str]] = [
    ("sms_provider", HIGH, lambda s: s.sms_provider == "console",
     "验证码只打日志不外发，而下发接口照样返回成功——居民端登录 100% 不可用且零报错",
     "配 MEDPLAT_SMS_PROVIDER=http 与网关地址/密钥"),
    ("wechat_provider", HIGH, lambda s: s.wechat_provider == "mock",
     "微信登录走本地桩。生产已有硬门拒绝换码（app/wechat.py），所以不是漏洞，"
     "但意味着微信登录这条路是通不了的",
     "配 official + appid/secret/回调域名，或确认本县不开放微信登录"),
    ("pii_encryption_enabled", HIGH, lambda s: not s.pii_encryption_enabled,
     "身份证/手机号明文落库",
     "先跑 scripts/pii_encrypt_backfill.py 回填，再置 true（顺序不能反，见运维手册 §12）"),
    ("log_file", HIGH, lambda s: not s.log_file,
     "日志仅 stdout，等保要求的 6 个月留存无法满足",
     "配 MEDPLAT_LOG_FILE 指向持久卷上的路径"),
    ("clamd_address", HIGH, lambda s: not s.clamd_address,
     "附件不做病毒扫描，一律标记 skipped",
     "配 clamd 地址（host:port 或 unix:/path）"),
    ("audit_anchor_webhook_url", HIGH, lambda s: not s.audit_anchor_webhook_url,
     "审计链只有本地锚点——有库权限的人截断链尾后对不出来",
     "配外部锚点 webhook，把链尾哈希异机存证"),
    ("alert_webhook_url", HIGH, lambda s: not s.alert_webhook_url,
     "定时任务与备份失败没有任何主动告警出口",
     "配告警 webhook（见 app/alerting.py）"),
    ("totp_required_roles", HIGH, lambda s: not s.totp_required_roles,
     "双因素认证未对任何角色强制",
     '按等保要求配置，如 "admin,director"'),
    ("db_pool_timeout_seconds", HIGH, lambda s: not s.db_pool_timeout_seconds,
     _pool_timeout_risk(),
     "配一个明确的超时秒数，让过载变成明确失败而不是无限等待"),
    ("session_idle_timeout_seconds", ADVISORY, lambda s: not s.session_idle_timeout_seconds,
     "无空闲超时，登录会话只受令牌有效期限制",
     "按等保要求配置滑动超时秒数"),
    ("audit_log_archive_days", ADVISORY, lambda s: not s.audit_log_archive_days,
     "审计留痕不自动归档截断，长期只涨不清",
     "按留存期配置（归档导出后才截断，见 app/routers/jobs.py）"),
    ("access_log_archive_days", ADVISORY, lambda s: not s.access_log_archive_days,
     "访问留痕不自动归档截断",
     "同上"),
    ("db_pool_size", ADVISORY, lambda s: not s.db_pool_size,
     "沿用 SQLAlchemy 默认连接池（pool_size=5），未按 worker 数与并发调过",
     "pool_size = worker 数 × 每 worker 并发 ÷ 实例数"),
]

#: 这些不是「默认值危险」，而是**配了反而危险**，单独一档。
DANGEROUS_IF_SET: list[tuple[str, object, str]] = [
    ("portal_legacy_verify", lambda s: s.portal_legacy_verify,
     "免登录的证件号查询面被显式打开（TECH_DEBT P1-3 已把它默认关掉）"),
    ("sms_debug_echo", lambda s: s.sms_debug_echo,
     "验证码回显开关被打开——生产另有硬门不会真回显，但这个值本身不该出现在生产配置里"),
]

#: 脚本查不到的东西。列出来是为了防止「跑过体检 = 可以上线」这个误解。
NOT_COVERED = [
    "备份能不能真的恢复回来——要在真 PG 上跑通 backup → restore_drill_pg 全链",
    "归档延迟具体多少秒（RPO≤15min 的达标依据），无真实归档目标测不出",
    "四个外部通道能不能真的联通——要厂商测试账号",
    "国产库（达梦/金仓/麒麟）能不能跑起来——本仓库零适配代码，纯部署期工作",
    "Redis / 数据库 / 杀毒服务是否**真的可达**（这里只看配没配，没有发起连接）",
]


def _acked() -> set[str]:
    raw = os.environ.get("MEDPLAT_PREFLIGHT_ACK", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def main() -> int:
    acked = _acked()
    print("=" * 72)
    print("割接前配置体检")
    print("=" * 72)
    settings, load_error = _load_settings()
    if settings is None:
        print()
        print("🔴 配置校验没通过——这套配置**连启动都启动不了**，比任何高危未决都更早：")
        print(f"   {load_error}")
        print()
        print("先把上面这条修掉再重跑本脚本。")
        return 1
    print(f"环境标识：MEDPLAT_ENV={settings.env!r} / MEDPLAT_ENVIRONMENT={settings.environment!r}"
          f"  →  {'生产' if settings.is_production else '**非生产**'}")
    if not settings.is_production:
        print()
        print("⚠️  当前不是生产环境。下面查的是**本机**的值，不是目标环境的值——")
        print("   请在割接目标机、带着真实环境变量重跑，否则这份报告没有意义。")
    print()

    unresolved: list[str] = []
    for name, level, is_default, risk, advice in CHECKS:
        value = getattr(settings, name)
        if not is_default(settings):
            print(f"✅ {name:32s} = {value!r}")
            continue
        if name in acked:
            print(f"➖ {name:32s} 取默认值，已登记为本县不需要（ACK）")
            continue
        mark = "🔴" if level == HIGH else "⚠️ "
        print(f"{mark} {name:32s} = {value!r}   [{level}]")
        print(f"     后果：{risk}")
        print(f"     建议：{advice}")
        if level == HIGH:
            unresolved.append(name)

    for name, is_set, risk in DANGEROUS_IF_SET:
        if is_set(settings):
            print(f"🔴 {name:32s} = {getattr(settings, name)!r}   [配了反而危险]")
            print(f"     后果：{risk}")
            unresolved.append(name)

    if not os.environ.get("MEDPLAT_REDIS_URL", ""):
        # 不是 Settings 字段（与 config.py 同一处理方式），单独查。
        print("⚠️  MEDPLAT_REDIS_URL              未配置   [建议]")
        print("     后果：登出令牌黑名单与登录防爆破锁定仅在本进程内存生效；"
              "多实例部署下这是安全事故（那种形态启动时会被直接拒绝）")

    print()
    print("-" * 72)
    print("本脚本**查不到**的（不要把「体检通过」当成「可以上线」）：")
    for item in NOT_COVERED:
        print(f"  · {item}")
    print("-" * 72)

    if not settings.is_production:
        print("\n结论：非生产环境，本次仅作预演。")
        return 2
    if unresolved:
        print(f"\n结论：{len(unresolved)} 项高危未决 —— {'、'.join(unresolved)}")
        print("逐项拍板后再割接；确实不需要的，用 MEDPLAT_PREFLIGHT_ACK 登记"
              "（并把理由写进变更单）。")
        return 1
    print("\n结论：高危项均已配置或已登记。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
