"""接口标准治理——棘轮测试（ratchet）。

    混乱代码  →  标准接口   ，逐块治理，只进不退。

本仓库有 881 个端点，历史上仅 ~14% 声明了 `response_model`（响应契约）。一次性
全补不现实，也违反"改动限定在任务本身"。于是用棘轮：**记录当前欠账基线，断言
它永不变大**。效果——

* 新写端点漏 `response_model` → 欠账变大 → 本测试变红，逼着补上；
* 每治理一个模块 → 欠账变小 → 把 `BASELINE_WITHOUT_RESPONSE_MODEL` 下调到新值；
* 已治理完的模块（`FULLY_GOVERNED`）若被改回裸 dict → 单独变红，防止回退。

`FULLY_GOVERNED` 本身曾是一份**需要人记得去补**的清单：某个模块碰巧全部端点
都带了契约，却没人把它登记进来，于是它后来被改回裸 dict 时不会单独变红——
只要总欠账没顶破基线（比如别处刚好治理了一个端点，一增一减净持平），
这次回退就**静默**发生了。实测这份清单已经落后现实 3 个模块
（auth / dispense / encounters）。

修法是让它跟着代码结构走：真实的"零欠账模块"集合由 `_coverage()` 从路由
元数据算出来，`test_已治理模块清单不许落后现实` 要求登记表与它**逐字相等**。
清单于是从"要靠人记得"变成"对不上就红"：模块治理干净了没登记 → 红；
登记了却回退 → 红（另一条）。两个方向都关上，人不必再记得什么。

这与仓库既有的"欠账不许变长"用例（如 test_stage15 的机构归属欠账）是同一手法。

治理配方见 docs/接口标准与治理.md：先给端点写特征化网钉住当前 JSON，再加
`response_model`（字段与现输出一一对应，保持字节不变），跑绿后回来把基线下调。
"""
from __future__ import annotations

import importlib
import pkgutil
import warnings

from fastapi import APIRouter
from fastapi.routing import APIRoute

import app.routers as platform_routers
import app.spd.routers as spd_routers

# —— 棘轮基线 ——
# 当前无 response_model 的 /api 端点数。**只允许调小，不允许调大。**
# 每治理一个模块就把这个数字改小（配合 FULLY_GOVERNED）。轨迹：881 中 757 → 756（checkups）→ 753（certs）→ 749（knowledge）→ 745（notifications）→ 743（infectious alerts/late-reports）→ 742（dictionaries import）→ 741（encounters /archive/360 全景视图，嵌套九段逐段建模）→ 740（performance /orgs 机构计分卡，动态 weights + 混形状 detail）。→ 732（ADR-0006 搬家后补齐 cssd cost-* 3 个与 performance improvements* 5 个，两模块回归 FULLY_GOVERNED）。
# → 724（B2 运营闭环：printing 全模块补契约——HTML 单据以 response_model=str 声明
# "text/html 字符串"契约、模板两端点建 Pydantic 模型；labqc/checkups 新端点生而全契约）。
# → 719（决策驾驶舱 metrics 全模块补契约：五个端点。三处建模判断都先实测再决定——
#   `*_pct` 恒为 float（分母 0 时返回字面量 0.0，不是 round 出来的 int）、
#   `by_level` 键由数据决定故宽键、`drilldown.items` 是八种行形状的真多态故
#   dict[str, Any]（同响应的 `fields` 自描述，用例钉住两者相等）。
#   46 个请求加契约前后逐字节一致，见 test_metrics_contract.py。）
# → 709（analytics 全模块补契约：十个端点。核心判断是 `round()` 与 Money 列派生的
#   数值一律 `int | float` 而非 float——实测同一个 `total_amount` 字段一行返回
#   `1234.5`、另一行返回 `100`（int）；写成 float 会把 `100` 变 `100.0`，即改字节
#   （变异验证实测到了这一字节差）。`performance-report` 的 items 是多态的
#   （失败项多一个 `error` 键），逐字段建模会给成功项注入 `"error": null`，
#   同样改字节，故用宽字典。17 个请求加契约前后逐字节一致，见 test_analytics_contract.py。）
# → 706（reports 三端点。**其中两个是 CSV 下载**，`response_model` 对它们没有意义
#   （函数直接返回 Response 对象，FastAPI 会跳过模型），故棘轮的判据同步放宽为
#   "有 response_model **或**显式声明了非 JSON 媒体类型"——判据从路由推导，不是
#   手工豁免清单。放宽没有白送任何端点：写这条时全仓库只有 printing 的 12 个端点
#   声明过 response_class，且它们本来就都有 response_model
#   （`test_放宽媒体类型口径没有白送任何端点` 钉住）。
#   把永远还不掉的账算进欠账，数字就不再表示"还有多少接口没契约"。）
# → 696（portal 的 auth 组 8 个 + 两个公开列表。**没做完整个 portal**：
#   `me`(19) 与 `spd`(26) 另批——`my-archive`/`surveys` 三个遗留端点与 `me/archive`
#   共用 `_build_archive`，拆开做会把同一个形状建模两次，故留给 me 那批一起做。
#   本批的关键是两个**条件键**：`auth/sms/code` 的 `debug_code` 与
#   `auth/wechat/authorize` 的 `mock_code`——声明成带默认值的可选字段会给**每一个**
#   响应注入 `null`，既改字节又等于公告该字段存在（`debug_code` 是登录验证码的
#   回显口子，P0 整改专门收紧过）。两个端点用 `response_model_exclude_unset=True`，
#   两条分支都做了逐字节比对，见 test_portal_auth_contract.py。）
BASELINE_WITHOUT_RESPONSE_MODEL = 696

# 已完成治理（全部端点声明契约）的模块——这些不许回退。治理新模块后加进来。
FULLY_GOVERNED = {
    "contracts",
    "cssd",
    "organizations",
    "referrals",
    "telemedicine",
    "checkups",  # 样板迁移，见 test_checkups_characterization.py
    "certs",  # 见 test_certs_characterization.py
    "knowledge",  # 见 test_knowledge_characterization.py
    "notifications",  # 见 test_notifications_characterization.py
    "infectious",  # 见 test_infectious_alerts_characterization.py
    "dictionaries",  # 见 test_dictionaries_characterization.py
    "performance",  # 见 test_performance_orgs_contract.py、test_cssd_improvement_contracts.py
    "consents",  # E2 个保法新模块，生而全契约，见 test_consents.py
    "printing",  # B2 全模块补契约：HTML 单据 response_model=str，模板端点建模，见 test_printing_documents.py
    "labqc",  # B2 室内质控新模块，生而全契约，见 test_labqc_westgard.py
    "metrics",  # 决策驾驶舱五端点，见 test_metrics_contract.py
    "analytics",  # 决策指标扩展十端点，见 test_analytics_contract.py
    "reports",  # /monitoring 走 Pydantic；两个 CSV 导出以 CsvResponse 声明媒体类型，
                # 见 test_reports_contract.py
    # 以下三个是"清单落后现实"的存量：它们早就零欠账，却一直没人登记，
    # 于是这些模块的回退一直不会单独变红。由 test_已治理模块清单不许落后现实 补上并钉住。
    "auth",
    "dispense",
    "encounters",
}


def _iter_endpoints():
    """遍历所有源路由模块里的 APIRoute（环境无关，不依赖 app.routes 的运行期封装）。"""
    for pkg in (platform_routers, spd_routers):
        for modinfo in pkgutil.iter_modules(pkg.__path__):
            if modinfo.name.startswith("_"):
                continue
            module = importlib.import_module(f"{pkg.__name__}.{modinfo.name}")
            for router in (v for v in vars(module).values() if isinstance(v, APIRouter)):
                for route in router.routes:
                    if not isinstance(route, APIRoute):
                        continue
                    if not (route.methods - {"HEAD", "OPTIONS"}):
                        continue
                    yield modinfo.name, route


def _declares_non_json_media(route) -> bool:
    """端点是否**显式声明了非 JSON 的响应媒体类型**（CSV/文件下载这类）。

    为什么这也算"声明了契约"：直接返回 `StreamingResponse` 的端点，
    `response_model` 对它没有意义——函数不返回可序列化对象，FastAPI 也会跳过模型。
    把这类端点永远算作欠账，等于往棘轮里掺进一笔**永远还不掉的账**，
    数字就不再表示"还有多少接口没契约"。它们的契约是"我返回 text/csv 字节流"，
    在 `responses` 里写明媒体类型就是把这句话写进 OpenAPI，是真的声明，不是豁免。

    判据从**路由对象推导**，不是手工清单——这是本仓库最近的治理方向
    （见"坏清单改自动推导"那批守卫）。门槛定在"必须写出媒体类型"而不是
    "设了 response_class 就算"：后者对 StreamingResponse 根本不写媒体类型，
    等于什么都没声明。

    放宽这个口径**没有让任何端点白捡"已治理"**：写这条时全仓库只有 printing 的
    12 个端点声明过 response_class，而它们本来就都有 response_model
    （`test_放宽媒体类型口径没有白送任何端点` 钉住这一点）。
    """
    from fastapi.datastructures import DefaultPlaceholder

    response_class = getattr(route, "response_class", None)
    if not isinstance(response_class, DefaultPlaceholder):
        media = getattr(response_class, "media_type", None)
        if media and not str(media).startswith("application/json"):
            return True
    for spec in (getattr(route, "responses", None) or {}).values():
        content = (spec or {}).get("content") or {}
        if any(not str(ct).startswith("application/json") for ct in content):
            return True
    return False


def _has_contract(route) -> bool:
    """声明了响应契约：Pydantic 模型，或显式的非 JSON 媒体类型。"""
    return route.response_model is not None or _declares_non_json_media(route)


def _coverage():
    total = 0
    without = 0
    per_module: dict[str, list[int]] = {}
    for mod, route in _iter_endpoints():
        total += 1
        per_module.setdefault(mod, [0, 0])
        per_module[mod][0] += 1
        if not _has_contract(route):
            without += 1
            per_module[mod][1] += 1
    return total, without, per_module


def test_放宽媒体类型口径没有白送任何端点():
    """把"声明了非 JSON 媒体类型"也算作有契约，只能让**真的写了声明**的端点脱账。

    这条盯的是口径本身会不会变成漏洞：凡是靠媒体类型算作已治理的端点，
    必须真的在 `responses` 里写出了媒体类型。顺带钉住数量——
    悄悄给一批端点挂上空的 `responses` 来刷低欠账，会让这里的清单变长。
    """
    by_media = sorted(
        f"{mod} {sorted(route.methods - {'HEAD', 'OPTIONS'})[0]} {route.path}"
        for mod, route in _iter_endpoints()
        if route.response_model is None and _declares_non_json_media(route)
    )
    assert by_media == [
        "reports GET /api/reports/monitoring/export",
        "reports GET /api/reports/operations/export",
    ], (
        f"靠媒体类型算作已治理的端点清单变了：{by_media}。"
        "新增这类端点是可以的，但必须是真的返回非 JSON 的下载/单据类接口，"
        "并在此处同步——别拿空 responses 刷低欠账。"
    )


def test_响应契约欠账不许变大():
    total, without, _ = _coverage()
    assert without <= BASELINE_WITHOUT_RESPONSE_MODEL, (
        f"无 response_model 的端点从基线 {BASELINE_WITHOUT_RESPONSE_MODEL} 涨到 {without}"
        f"（共 {total}）。新端点必须声明 response_model；见 docs/接口标准与治理.md。"
    )


def test_基线保持收紧不放水():
    # 若欠账已降到基线以下，说明有人治理了模块却没同步下调基线——提醒把基线改小，
    # 保持棘轮"咬得紧"。这条允许有 0 容差地提示，但不阻断（用 !=  的软提示）。
    _, without, _ = _coverage()
    assert without <= BASELINE_WITHOUT_RESPONSE_MODEL
    # 允许等于；小于时打印提示（不失败），引导下调基线。
    if without < BASELINE_WITHOUT_RESPONSE_MODEL:
        print(
            f"\n[提示] 欠账已降到 {without}，请把 BASELINE_WITHOUT_RESPONSE_MODEL "
            f"从 {BASELINE_WITHOUT_RESPONSE_MODEL} 下调到 {without}。"
        )


def test_已治理模块不许回退():
    _, _, per_module = _coverage()
    regressed = {
        m: per_module.get(m, [0, 0])[1]
        for m in FULLY_GOVERNED
        if per_module.get(m, [0, 0])[1] != 0
    }
    assert not regressed, (
        f"已治理模块出现无契约端点（回退）：{regressed}。这些模块应保持全部端点声明 response_model。"
    )


def _fully_governed_in_reality(per_module: dict[str, list[int]]) -> set[str]:
    """从路由元数据算出"当前真的零欠账"的模块集合——不手工维护。"""
    return {m for m, (_total, without) in per_module.items() if without == 0}


def test_已治理模块清单不许落后现实():
    """`FULLY_GOVERNED` 必须**逐字等于**真实的零欠账模块集合。

    少登记（清单落后现实）：该模块日后回退时不会单独变红，只要总欠账没顶破
    基线就静默过去——这正是"要靠人记得更新才正确"的坏清单。
    多登记（清单超前现实）：由 `test_已治理模块不许回退` 报出来。
    两条合起来，这份清单等价于从代码结构推导，人不需要再记得什么。
    """
    total, without, per_module = _coverage()
    reality = _fully_governed_in_reality(per_module)
    summary = "\n".join([
        "",
        "[接口契约棘轮] 覆盖面自证",
        f"  分母：源路由模块 {len(per_module)} 个 / 端点 {total} 个"
        "（app.routers + app.spd.routers 全量遍历，无抽样、无跳过）",
        f"  契约欠账：{without}（基线 {BASELINE_WITHOUT_RESPONSE_MODEL}）"
        f"    已治理端点：{total - without}    覆盖率 {(total - without) * 100 / total:.1f}%",
        f"  零欠账模块：实测 {len(reality)} 个 / 清单登记 {len(FULLY_GOVERNED)} 个"
        f"    未登记 {len(reality - FULLY_GOVERNED)} 个    登记了却已回退 "
        f"{len(FULLY_GOVERNED - reality)} 个",
    ])
    print(summary)
    warnings.warn(summary, UserWarning, stacklevel=2)

    missing = sorted(reality - FULLY_GOVERNED)
    assert missing == [], (
        f"以下模块已经零契约欠账，但没登记进 FULLY_GOVERNED：{missing}。"
        " 不登记 = 它日后被改回裸 dict 时不会单独变红（总欠账一增一减就掩盖过去了）。"
        " 治理完一个模块，就把它加进清单。"
    )
