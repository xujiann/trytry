"""全平台孤儿端点棘轮：每个 `/api` 端点都要有前端调用点，或写明为什么不需要。

## 为什么把 care 那条守卫扩到全平台

`test_spd_care_frontend_coverage.py` 立桩时的情形值得记住：care.py 的 22 条路径
**后端交付了、需求对照表逐条写着"已实现"**，却没有一个界面调用它们，医生工作台
还在展示一个恒为 0 的"待回复咨询"计数。那条守卫只守 care 一个模块。
2026-09-15 按同一判据量全平台：**730 条路径里 200 余条没有任何前端调用形态**
（精确数字见下方 `KNOWN_ORPHANS` 的长度，由本文件自证）——三张对照表
（指引 36 项 / 招标 163 条 / 系统功能清单）里的"已实现"，有相当一部分只是"有端点"。

孤儿端点的坏处有两面：使用者以为功能存在，攻击者拿到的却是一片没人走过的接口面。
所以 `docs/功能完善开发规则.md` 把「每个端点有入口」列为模块功能完整的第一项，
本文件是它的闸门。

## 判据（与 care 那条同源，两处收紧）

分母复用 `test_api_contract_governance._iter_endpoints()`——**不重写枚举**，
它已经处理过 `app.routes` 上 `_IncludedRouter` 封装、spd 子包递归、同名模块合并
这几个坑（第一次量时用 `app.routes` 直接数，只数出 `/api/health` 一条）。

语料是三端全部脚本与页面（管理端 `*.js`、`verify.html`、居民端与医生端 `m/*`）。
匹配的是**源码里的调用形态**：`/api/spd/consults/${id}/reply` 这种模板字面量。
路径参数位置接受 `${…}` 插值或写死的数字，其余逐字匹配。两处比 care 收紧：

* **路径必须在此结束**（后面不能再接 `/`、字母、`.`）：只调过 `/api/spd/tasks/${id}/assign`
  不算调过列表 `/api/spd/tasks`。care 的写法没有右边界，列表端点会被子资源的调用
  "顺带算作有入口"——那正是"判据比缺陷窄"的一种。
* 语料把 `*.html` 也算上：`verify.html` 里的防伪验真是直接 `fetch("/api/print/verify…")`。

已量过的盲区：字符串拼接（`"/api/x/" + id`）在三端**0 处**，所以没有为它放宽判据。
若将来出现，别改正则——改成模板字面量，两种写法只留一种。

## 三张名单，各管一件事

* `EXEMPT_MODULES` / `EXEMPT_PATHS`：**按设计不需要界面**的端点，每条写明理由。
  三类：对机器不对人（HL7/FHIR、支付回调、设备批量回传）；已废弃或已被聚合接口取代、
  只留给旧客户端的路径（接新界面是倒退）；地址由后端字段给出、前端按变量请求的路径
  （字面匹配看不见，入口在——登记时写明是哪个字段、哪处调用）。只许变少。
* `KNOWN_ORPHANS`：**该有界面但还没有**的端点——欠账名单，只许变少。
  它是显式名单而不是一个计数：计数会让"补上一个、新漏一个"净持平而不红
  （契约闸门早年就吃过这亏，见其模块 docstring）。
* 两个方向都钉：新孤儿不在名单里 → 红；名单里的已经接上界面 → 也红（把它划掉）。
  名单于是从"要靠人记得"变成"对不上就红"。

## 它不保证什么

守的是**入口存在**，不是入口好用：按钮点了 500、表单字段对不上、`prompt()` 录入，
都不在这里。行为由后端用例守，渲染由 `scripts/render_diff.js` 守。
"""
from __future__ import annotations

import re
from pathlib import Path

import test_api_contract_governance as contract
from test_spd_care_frontend_coverage import EXEMPT as CARE_EXEMPT

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def frontend_files() -> list[Path]:
    """三端全部前端源码：管理端脚本与页面、居民端/医生端 H5。"""
    return (
        sorted(STATIC.glob("*.js"))
        + sorted(STATIC.glob("*.html"))
        + sorted((STATIC / "m").glob("*.js"))
        + sorted((STATIC / "m").glob("*.html"))
    )


def frontend_corpus() -> str:
    return "\n".join(p.read_text("utf-8") for p in frontend_files())


def pattern_for(path: str) -> re.Pattern[str]:
    """把路由模板变成"前端怎么写这个调用"的正则。

    `{param}` → `${…}` 插值或数字字面量；其余段逐字匹配；**路径在此结束**
    （右侧不能再接路径字符），否则列表端点会被子资源的调用顶替。
    """
    parts = [
        re.escape(seg) if not seg.startswith("{") else r"(\$\{[^}]*\}|\d+)"
        for seg in path.strip("/").split("/")
    ]
    return re.compile("/" + "/".join(parts) + r"(?![A-Za-z0-9_./-])")


def endpoint_paths() -> dict[str, str]:
    """`/api` 下每条不同路径 → 所属路由模块（`billing` / `spd/config` 这种包限定名）。

    分母来自契约闸门的枚举器，理由见模块 docstring。同一路径多个动词只算一条——
    界面调的是路径，动词由后端用例守。
    """
    paths: dict[str, str] = {}
    for module, route in contract._iter_endpoints():
        if route.path.startswith("/api/"):
            paths.setdefault(route.path, module)
    return paths


def orphan_paths(corpus: str | None = None) -> set[str]:
    """裸测量：没有任何前端调用形态的路径（不扣豁免、不扣欠账名单）。"""
    corpus = frontend_corpus() if corpus is None else corpus
    return {path for path in endpoint_paths() if not pattern_for(path).search(corpus)}


#: 整模块按设计没有界面：对机器不对人。与 `test_api.py::test_spa_covers_every_backend_module`
#: 的模块级白名单同一口径（那条守"模块有页面"，本文件守"每条路径有入口"）。
EXEMPT_MODULES: dict[str, str] = {
    "integration": "HL7 v2 / FHIR R4 入站转换与导出——对接院内系统与省平台的机器接口，没有人点的界面",
}

#: 单条路径按设计没有界面。只许变少；新增须写明**为什么这个端点不需要界面**。
EXEMPT_PATHS: dict[str, str] = {
    **CARE_EXEMPT,  # 设备/物联网批量回传（care 那条守卫登记的，理由见原处）
    "/api/billing/payments/callback": "支付网关的异步回调（签名验证 + 幂等落账），由网关服务器调用，不是页面",
    # 第二类：已废弃 / 已被聚合接口取代的路径——接新界面是倒退，不是补齐
    "/api/portal/my-archive": (
        "【已废弃，deprecated=True】无账户体系的过渡通道（身份证号入参 + _require_legacy_enabled 开关），"
        "界面走登录态 GET /me/archive；给它加入口等于把证件号重新放回 query"
    ),
    "/api/portal/surveys": "【已废弃，deprecated=True】同上的过渡通道，界面走登录态 POST /me/surveys",
    "/api/portal/me/referrals": (
        "已由 GET /me/referrals/all 取代（ADR-0003 聚合，可 source=platform 收窄），两页界面都走聚合；"
        "保留给旧客户端并已标 deprecated=True"
    ),
    "/api/portal/spd/referrals": (
        "已由 GET /me/referrals/all?source=spd 取代（同上），居民端慢专病·转诊页走聚合；"
        "保留给旧客户端并已标 deprecated=True"
    ),
    # 第三类：地址由后端字段给出、前端按变量请求——字面匹配看不见，但入口真实存在
    "/api/portal/spd/referrals/{case_id}": (
        "聚合列表每条带 detail_path=/api/portal/spd/referrals/{id}?patient_id=…（spd/service.py），"
        "居民端 bindReferralDetails 按该字段请求「查看全过程」；本闸门按字面匹配看不到变量地址"
    ),
}


def unexplained_orphans(corpus: str | None = None) -> set[str]:
    """扣掉书面豁免之后的孤儿——这才是欠账。"""
    modules = endpoint_paths()
    return {
        path for path in orphan_paths(corpus)
        if path not in EXEMPT_PATHS and modules[path] not in EXEMPT_MODULES
    }


#: 欠账名单：**该有界面但还没有**的路径。只许变少。
#:
#: 按模块分组写，方便按 `docs/模块完成度.md` 挑批次；每接通一条就把它从这里划掉
#: （不划掉 `test_登记的孤儿仍然是孤儿` 会红）。别把它当豁免用——按设计不需要
#: 界面的走上面 `EXEMPT_*` 并写理由。
KNOWN_ORPHANS: set[str] = {
    # admin_mgmt（1）——两条物资的已接通（第十六批）；剩下这条不是"还没做界面"：
    # 它与 /api/staffing/secondments/{id}/end 写同一张 secondments 表，而后者更全
    # （不送日期默认今天、校验结束日不早于开始日、返回 18 键台账行）且**早有界面**
    # （pages-mgmt.js 派驻台账的「结束派驻」）。再给它接一个入口，等于让同一个动作
    # 有两条校验不同的路——所以不接，也不豁免（豁免是"按设计不需要界面"，这条不是）。
    # 该怎么收口是架构裁定，已登记 docs/待裁定事项清单.md「mgmt / staffing 派驻两套实现」。
    "/api/mgmt/secondments/{secondment_id}/end",
    # auth（3）——**按 CLAUDE.md §8 暂缓，不是漏做**：这三条是双因素（TOTP）的
    # 开通/验证/关闭，属"涉及认证的改动，走 §9 的 ADR 并请人复核"。后端已完整
    # （setup 生成 pending 密钥 → activate 验一次启用 → disable 须验当前码才能关），
    # 缺的只是账号页上的入口；但接入口意味着把密钥与 otpauth URI 印到页面上，
    # 这一步该由人确认，不该由一次自动批次顺手做掉。**清到这里请先找人复核。**
    "/api/auth/totp/activate",
    "/api/auth/totp/disable",
    "/api/auth/totp/setup",
    # dictionaries（1）
    "/api/dictionaries/{system_code}/import",
    # eldercare（1）
    "/api/eldercare/stats",
    # inpatient（1）
    "/api/inpatient/orders/{order_id}/executions",
    # metrics（1）
    "/api/metrics/drilldown-metrics",
    # org_groups（1）
    "/api/org-groups/of-org/{org_id}",
    # organizations（1）
    "/api/organizations/tree-health",
    # patients（1）
    "/api/patients/{ehc_no}",
    # resources（1）
    "/api/resources/{resource_id}",
    # surveillance（1）
    "/api/surveillance/resources/{resource_id}",
    # tcm（2）
    "/api/tcm/constitution",
    "/api/tcm/constitution/spec",
    # tcm_heritage（1）
    "/api/tcm-heritage/simulations/{case_id}/attempts",
    # vaccine_supply（2）
    "/api/vaccine-supply/aefi/{report_id}/outcome",
    "/api/vaccine-supply/expiring",
}


def test_没有新的孤儿端点():
    fresh = sorted(unexplained_orphans() - KNOWN_ORPHANS)
    assert not fresh, (
        "以下端点没有任何前端调用点，且不在欠账名单里——新端点要么带界面来，"
        "要么按设计不需要界面就进 EXEMPT_PATHS 并写明理由：\n  " + "\n  ".join(fresh)
    )


def test_登记的孤儿仍然是孤儿_修好了就从名单里划掉():
    """两个方向都钉：接上界面的不划掉 → 红；路径改名/删除了还挂在名单上 → 红。"""
    paths = endpoint_paths()
    gone = sorted(p for p in KNOWN_ORPHANS if p not in paths)
    assert not gone, f"这些路径已不存在（改名或删除），从 KNOWN_ORPHANS 里划掉：{gone}"
    wired = sorted(KNOWN_ORPHANS - unexplained_orphans())
    assert not wired, (
        "这些端点已经有前端调用点了——把它们从 KNOWN_ORPHANS 里划掉（欠账只许变少，"
        "但要看得见它在变少）：\n  " + "\n  ".join(wired)
    )


def test_豁免仍然成立():
    """豁免不许腐烂：豁免的路径必须仍是端点、且仍然没人调；模块必须仍然存在；
    豁免与欠账不许重叠（一条路径不能既"按设计不需要"又"欠着"）。"""
    paths = endpoint_paths()
    modules = set(paths.values())
    raw = orphan_paths()
    for path, why in EXEMPT_PATHS.items():
        assert why.strip(), f"{path} 的豁免没写理由"
        assert path in paths, f"豁免的 {path} 已不是端点，划掉"
        assert path in raw, f"豁免的 {path} 其实已经有前端调用了——豁免过期，划掉"
    for module, why in EXEMPT_MODULES.items():
        assert why.strip(), f"{module} 的豁免没写理由"
        assert module in modules, f"豁免的模块 {module} 已不存在，划掉"
    overlap = set(EXEMPT_PATHS) & KNOWN_ORPHANS
    assert not overlap, f"既豁免又欠账：{sorted(overlap)}"
    assert len(EXEMPT_PATHS) <= 7 and len(EXEMPT_MODULES) <= 1, (
        "豁免只许变少：新增前先问这个端点是不是真的没有人会点"
    )


def test_分母与语料都没有空转():
    """反空转自检：分母缩水（枚举器坏了、spd 子包没扫到）或语料读空（目录改名），
    上面三条会全绿——那是最危险的绿。"""
    paths = endpoint_paths()
    assert len(paths) >= 700, f"只枚举到 {len(paths)} 条路径，分母可疑"
    assert any(m == "spd/config" for m in paths.values()), "没扫到 spd/config 子包"
    files = {p.name for p in frontend_files()}
    assert {"core.js", "m.js", "doctor.js", "verify.html"} <= files, f"语料缺文件：{files}"
    corpus = frontend_corpus()
    assert corpus.count("/api/") >= 700, "前端语料里的 /api/ 字面量异常少，可能读错目录"


def test_匹配器认得三种写法_且路径必须在此结束():
    """判据自证——这一轮学到的：先证明尺子量得准，再用它量。"""
    p = pattern_for("/api/spd/tasks/{task_id}/assign")
    assert p.search("api(`/api/spd/tasks/${t.id}/assign`, {")
    assert p.search('api("/api/spd/tasks/12/assign")')
    assert not p.search('api("/api/spd/tasks/assign")'), "参数位不能为空"
    lst = pattern_for("/api/spd/tasks")
    assert lst.search('api("/api/spd/tasks")') and lst.search("api(`/api/spd/tasks?${qs}`)")
    assert not lst.search("api(`/api/spd/tasks/${id}/assign`)"), "只调过子资源不算调过列表"
    assert not lst.search('api("/api/spd/tasks-export")'), "前缀相同的另一条路径不算"
    svg = pattern_for("/api/spd/scales/{scale_id}/qr.svg")
    assert svg.search("`/api/spd/scales/${s.id}/qr.svg`")
    assert not pattern_for("/api/spd/scales/{scale_id}").search("`/api/spd/scales/${s.id}/qr.svg`")
