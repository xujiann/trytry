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
# → 674（portal 的 me 组 19 个 + 三个已废弃的遗留端点，`app/routers/portal.py`
#   契约欠账清零。三处判断：①`me/family` 的条件键 `member_id`——本人那一行没有它
#   （本人不是一条代管关系），声明成可选字段会注入 `"member_id": null`，客户端
#   照着 null 调 `DELETE /me/family/None` 就是平白多出来的错误路径，故用
#   `response_model_exclude_unset=True`；②Money 陷阱在这一批出现了四处
#   （账单三项、费用明细单价与金额、分类汇总的**值**、押金余额），整数金额声明成
#   float 会把「200 元」变成「200.0 元」；③`_build_archive` 被三个端点共用，
#   只建一个 `ArchiveOut`，并有用例钉住三者形状相等，免得日后改一处漏两处。
#   消息两端点复用 notifications 已有的 `NotificationOut`/`UnreadCountOut`，
#   不另建同形模型。29 个请求加契约前后逐字节一致，四处变异各自转红，
#   见 test_portal_me_contract.py。）
# → 648（`spd/portal` 慢专病患者移动端 26 个端点，两侧居民端契约就此都清零）。
#   最要紧的是 `/screenings` 的**三种形状**：草稿+量表（带 answered/total_items）、
#   草稿无量表（只有四个键）、落库（带 id/result/can_apply）。逐字段建模会把三者
#   的字段互相注入 null，故 `response_model_exclude_unset=True`，三条分支各钉一遍。
#   `score` 是 `int | float`——有量表时 `round(total, 2)` 是 float，无量表时兜底
#   字面量 `0` 是 int。与平台侧相反，spd 这边多是 **Float 列**（measurement.value、
#   assessment.score），整数值读回来就是 `140.0`，声明 float 才是原样。
#   两处自己犯的错都由机制当场抓到，写进了模型 docstring：详情模型继承列表模型
#   凭空要求了 `created_at`（响应校验拦下）、`SpdScreeningOut` 字段顺序排错
#   （序列化按声明顺序走，逐字节比对拦下）。
#   41 个请求加契约前后逐字节一致，五处变异各自转红，见 test_spd_portal_contract.py。）
# → 635（`spd/config` 第一批：catalog 9 + centers 4。config 是个包（ADR-0008 拆的），
#   58 个端点分在 6 个子模块里，按子模块分批做——一次比 58 个端点，逐字节比对出了
#   问题不好定位，粒度本身就是这套办法的价值。三处判断：`ProgramDetailOut` 继承
#   `ProgramOut` 是对的（详情是列表的**严格超集**，只多 targets——与 spd/portal 那批
#   的转诊详情正相反，那个不是超集，继承就错了）；`target_low`/`target_high` 是可空
#   Float（定性目标没有上下限）；`org-tree` 是**自引用递归**模型（树深由数据决定）。
#   23 个请求逐字节一致，五处变异各自转红，见 test_spd_config_catalog_contract.py。）
# → 614（第二批：paths 7 + devices 9，外加**判据第二次放宽**——204 无响应体也算
#   声明了契约。`_template_out` 出三种形状（列表只带 node_count、详情/新建带
#   nodes+node_count、复制/改状态两个都不带），用 exclude_unset，且 nodes 必须
#   声明在 node_count 之前（序列化按声明顺序走）。`success_rate` 是 Float 列 +
#   round(...,2)，满分也是 100.0。204 那条**确实白送了 5 个端点**（与放宽媒体
#   类型那次不同，那次是 0 个），故 614 = 619 - 5；由
#   test_204口径没有白送别的端点 钉住清单。38 个请求逐字节一致，五处变异转红，
#   见 test_spd_config_paths_devices_contract.py。）
# → 588（第三批：scales 15 + teams 12，`spd/config` 58 个端点清零、进
#   FULLY_GOVERNED。四处判断：服务包 `price` 是 Money（Numeric）列，整数价
#   声明成 float 就把「200 元」变「200.0 元」；标签**新建与列表不同形**
#   （列表没有 active，它本身已按 active 过滤），两个模型不能合并；`_team_out`
#   出三种形状用 exclude_unset，且 member_count 声明在 members 之前；两个二维码
#   端点改用 `_base.SvgResponse`（与 reports.CsvResponse 同一写法，声明与实际
#   返回是同一个类）。41 个请求逐字节一致，五处变异各自转红——SVG 的字节数
#   随 token_urlsafe 每次都变，做过对照实验（同一份代码跑两次一样变），
#   比对时按令牌归一化，令牌写死的那个二维码前后完全一致。
#   见 test_spd_config_scales_teams_contract.py。）
# → 470（一次十二个模块：medwaste 11 / clinical_docs 9 / materials 9 / surgery 10 /
#   accounting 9 / disease_programs 9 / rbac 9 / surveillance 9 / tcm_heritage 9 /
#   workflows 9 / outpatient_docs 13 / fund 12，共 118 个端点，十二个模块全部清零。
#   覆盖率首次过半（50.26%）。
#   这一批换了**取证方式**：不再每个模块手写一份捕获脚本，改成给 app 装一个
#   中间件，把**整个测试套件**跑出来的每个响应按 (方法, 路由模板, 状态码) 记下
#   字节，加契约前后各跑一次逐项比对——一次覆盖所有模块。噪声底先做过对照实验
#   （同一份代码跑两次），把随机项（验证码、令牌、二维码内容、时间戳）归一化到
#   0 处差异后才开始用。工具见 tests/capture_plugin.py 与 docs/接口标准与治理.md。
#   建模判断沿用既有几类：Money 列一律 int | float（accounting 全模块金额、
#   materials 的采购与耗材单价）；条件键用 exclude_unset（accounting 凭证的
#   entries、rbac 内置角色的 note、tcm_heritage 决策点的 answer/explain——
#   最后这个是**嵌套**条件键，学员拉题目时答案整个键不出现）；
#   "新建回执"与"列表行"键集合不同的一律两个模型，不硬套继承。）
# → 450（ADR-0006 收官批：`service_extras` 拆解后落到七个模块的那 20 个端点。
#   `cssd` 在搬家那个提交里短暂移出 FULLY_GOVERNED——搬进去 3 个无契约端点让它
#   回退了，而**总欠账一点没变**（470→470，只是换了名下）：只有总基线的话这次
#   回退会完全静默，"已治理模块不许回退"那条盯的正是这个。本批补完即加回。
#   `surveys`/`triage` 是收官时新建的模块，生而全契约。
#   两处建模判断：`ExamResource.price` 是 Money 列（`int | float`，公示价
#   「120 元」不能变「120.0 元」）；`survey_stats` 的**字段顺序**照 handler 实际
#   出键排——它 `pop("count")` 之后又重新赋值，`count` 因此被挪到 `distribution`
#   与 `negative` 之后，照读起来顺眼的顺序排就是改字节。
#   见 test_service_extras_split_contract.py。）
# → 427（`spd/assess` 24 个端点里的 23 个。**`GET /api/spd/scores-analysis` 刻意
#   不加**：它两个分支的键集合与**键顺序都不同**（空数据 4 键 total/distribution/
#   top_deductions/average，有数据 5 键 total/average/distribution/top_deductions/
#   ranking），Pydantic 按声明顺序序列化，单个模型最多只能满足一个分支（实测确认）。
#   用宽字典是"拿它逃避契约"（这里没有任何东西自描述形状），改 handler 统一两分支
#   是行为变更、不该夹在契约批次里——故留在欠账并写明原因，由
#   test_scores_analysis_两分支形状不一致 钉住（统一了它就红，提醒补契约）。
#   一处**逐字节比对抓到的真改动**：`IndicatorPlanRefOut.weight` 取自方案的
#   **JSON 列** items（不是数据库 Float 列），实测存的是整数 100，声明成 float
#   会变 `100.0`——Money 陷阱的同一形状换了来源，判据仍是"实际存的是什么"。
#   见 test_spd_assess_contract.py。）
# → 396（`spd/care` 31 个端点，覆盖率 58.10%）。四处判断：`by_item` 是**两层
#   动态字典**（题目 key → 选项 → 计数，两层的键都由量表决定，逐字段建模等于
#   把某张量表写死进契约）；`trend.latest` 可为 null（"最近一次不存在"与
#   "最近一次是空的"是两回事）；`RevisitOut.items` 是 String 列不是 JSON 数组
#   （名字像数组，列类型说了算）；三处"新建回执与列表不同形"各建两个模型。
#   套件级比对落在 spd/care 内 0 处差异；7 个零覆盖端点另补了用例。
#   见 test_spd_care_contract.py。）
BASELINE_WITHOUT_RESPONSE_MODEL = 367

# 已完成治理（全部端点声明契约）的模块——这些不许回退。治理新模块后加进来。
FULLY_GOVERNED = {
    "contracts",
    "cssd",  # 上个提交因 ADR-0006 搬入 3 个无契约端点短暂移出，本批补完即加回
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
    # 两侧居民端。它们是**两个 key**（模块名按包限定，见 _iter_endpoints 的
    # docstring）——不分开的话，其中一方的回退不会单独变红。
    "portal",  # 见 test_portal_auth_contract.py、test_portal_me_contract.py
    "spd/portal",  # 见 test_spd_portal_contract.py
    # 配置域：包，58 个端点分 6 个子模块，分三批做完（catalog+centers /
    # paths+devices / scales+teams），见 test_spd_config_*_contract.py
    "spd/config",
    # ADR-0006 收官时新建的两个模块，生而全契约
    "surveys",
    "triage",
    # 慢专病照护域：31 个端点，见 test_spd_care_contract.py
    "spd/care",
    # 慢专病随访域：29 个端点，见 test_spd_followup_contract.py
    "spd/followup",
    # 以下十个模块由**套件级字节捕获**（tests/capture_plugin.py）一次性取证：
    # 加契约前后各跑一遍全套件，逐 (方法,路径,状态) 比对响应字节。
    "medwaste",
    "clinical_docs",
    "materials",
    "surgery",
    "accounting",
    "disease_programs",
    "rbac",
    "surveillance",
    "tcm_heritage",
    "workflows",
    "outpatient_docs",
    "fund",
    # 以下三个是"清单落后现实"的存量：它们早就零欠账，却一直没人登记，
    # 于是这些模块的回退一直不会单独变红。由 test_已治理模块清单不许落后现实 补上并钉住。
    "auth",
    "dispense",
    "encounters",
}


def _iter_endpoints():
    """遍历所有源路由模块里的 APIRoute（环境无关，不依赖 app.routes 的运行期封装）。

    模块名按包限定（spd 的加 `spd/` 前缀）。不加前缀时两个包里的同名模块会被
    合并成一个 key——现实里就有一对：`app/routers/portal.py` 与
    `app/spd/routers/portal.py`。合并的后果是**前者治理干净了也进不了
    `FULLY_GOVERNED`**（合并后的 key 还带着后者的欠账），于是它被改回裸 dict
    时不会单独变红，只剩总基线兜底——而总基线是可以被别处的治理抵消的。
    这正是本文件开头说要关掉的那种"静默回退"，只是换了个入口。
    """
    for pkg, prefix in ((platform_routers, ""), (spd_routers, "spd/")):
        for modinfo in pkgutil.iter_modules(pkg.__path__):
            if modinfo.name.startswith("_"):
                continue
            name = f"{prefix}{modinfo.name}"
            module = importlib.import_module(f"{pkg.__name__}.{modinfo.name}")
            for router in (v for v in vars(module).values() if isinstance(v, APIRouter)):
                for route in router.routes:
                    if not isinstance(route, APIRoute):
                        continue
                    if not (route.methods - {"HEAD", "OPTIONS"}):
                        continue
                    yield name, route


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


def _declares_empty_body(route) -> bool:
    """端点是否声明了**没有响应体**（HTTP 204）。

    与 CSV 下载同一个道理：204 按定义就没有 body，`response_model` 对它没有意义
    ——函数直接返回 `Response(status_code=204)`，FastAPI 也不会走模型。把这类
    端点永远算作欠账，等于往棘轮里掺进第二笔**永远还不掉的账**。

    「204」本身就是写进 OpenAPI 的契约声明（"这个接口成功时不返回内容"），
    不是豁免。判据同样从路由对象推导，不是手工清单。

    放宽这一条**确实白送了 5 个端点**（3 个 spd/config + spd/followup 与
    spd/population 各 1 个，都是删除接口，写这条时它们都没有 response_model）
    ——这与放宽媒体类型那次不同，那次是 0 个。数字因此一次性降 5，
    但降掉的是本来就还不掉的账。`test_204口径没有白送别的端点` 钉住这份清单，
    往后谁想靠改 status_code 刷低欠账，那条会变红。
    """
    return route.status_code == 204


def _has_contract(route) -> bool:
    """声明了响应契约：Pydantic 模型、显式的非 JSON 媒体类型，或 204 无响应体。"""
    return (
        route.response_model is not None
        or _declares_non_json_media(route)
        or _declares_empty_body(route)
    )


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
        # 两个二维码：`_base.SvgResponse` 同时是 response_class 与实际返回的类
        "spd/config GET /api/spd/scales/{scale_id}/qr.svg",
        "spd/config GET /api/spd/village-doctors/{vd_id}/qr.svg",
    ], (
        f"靠媒体类型算作已治理的端点清单变了：{by_media}。"
        "新增这类端点是可以的，但必须是真的返回非 JSON 的下载/单据类接口，"
        "并在此处同步——别拿空 responses 刷低欠账。"
    )


def test_204口径没有白送别的端点():
    """靠 204 脱账的端点必须是**真的不返回内容**的删除类接口，且清单是这几个。

    与媒体类型那条同样的用途：口径本身不能变成漏洞。给一个原本返回 JSON 的
    端点改成 `status_code=204` 会改响应字节（body 直接没了），不可能"顺手"发生；
    真发生了，这里的清单会变长，当场看得见。
    """
    by_204 = sorted(
        f"{mod} {sorted(route.methods - {'HEAD', 'OPTIONS'})[0]} {route.path}"
        for mod, route in _iter_endpoints()
        if route.response_model is None and _declares_empty_body(route)
    )
    assert by_204 == [
        "spd/config DELETE /api/spd/path-nodes/{node_id}",
        "spd/config DELETE /api/spd/path-templates/{template_id}",
        "spd/config DELETE /api/spd/team-members/{member_id}",
        "spd/followup DELETE /api/spd/report-tasks/{task_id}",
        "spd/population DELETE /api/spd/groups/{group_id}/members/{patient_id}",
    ], (
        f"靠 204 算作已治理的端点清单变了：{by_204}。"
        "新增这类端点是可以的，但必须真的是无响应体的删除接口，并在此处同步。"
    )


def test_两个包里的同名模块不被合并成一个key():
    """`app/routers/portal.py` 与 `app/spd/routers/portal.py` 必须是两个 key。

    这条是上面那个前缀的**反空转守卫**：把前缀去掉，两者合并成一个 `portal`，
    `spd/portal` 这个 key 根本不存在，本条当场转红。

    为什么这个前缀不是可有可无的整洁癖：两者现在都零欠账、都登记在
    `FULLY_GOVERNED` 里。合并成一个 key 之后，**其中一方回退成裸 dict 时另一方
    还撑着这个 key**——`without` 不为 0 才会红，可这个 key 的 without 是两者之和，
    只要没人去看总基线（而总基线可以被别处的治理抵消），回退就静默发生了。
    分成两个 key，谁退谁红。
    """
    _, _, per_module = _coverage()
    assert "portal" in per_module and "spd/portal" in per_module, sorted(per_module)
    for key in ("portal", "spd/portal"):
        assert per_module[key][1] == 0, (
            f"{key} 出现了 {per_module[key][1]} 项契约欠账——它已登记为零欠账模块"
        )
    # 两者的端点数都不为零：万一哪天某个包里的 portal.py 被搬走，这个 key 会
    # 变成 [0, 0]，上面的断言照样绿——那时这条守卫就空转了。
    assert per_module["portal"][0] > 0 and per_module["spd/portal"][0] > 0


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
