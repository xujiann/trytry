"""全平台「端点必须有前端调用点」棘轮——孤儿端点闸门。

## 这道闸门为什么从一个模块推到全平台

2026-08-27 补 `spd/care` 时的形状是：22 条路径 / 31 个端点**全部没有前端调用点**
——后端交付了、需求对照表把它们逐条算作已实现，而界面一个都没有。那次是靠人工
比对需求表发现的，事后立的桩（`test_spd_care_frontend_coverage.py`）只守 care
一个模块。**同形缺口当然不止一处**：本轮把判据铺到全部路由模块，当场又量出
`spd/config/teams.py` 的 12 个端点零调用点——服务团队与村医档案建不出来，
任务分派、随访、转诊这条基层执行链在真环境里一条都跑不起来。

孤儿端点的坏处有两面：使用者以为功能存在（需求表写着"已实现"），
攻击者拿到的却是一片没人走过的接口面。

## 分母怎么算（不许手写清单）

判据是「忘记更新它，会静默出错，还是当场变红？」——手写端点清单属于前者。
这里分母**现算**：`pkgutil.walk_packages` 遍历 `app/routers` 与 `app/spd/routers`
下的每一个模块，收集其中所有 `APIRouter` 上的 `APIRoute`，按对象身份去重
（`spd/config` 是个包，6 个子模块共用 `_base.router` 同一个对象，不去重会把
58 个端点数 6 遍），再按 **handler 所属模块**（`route.endpoint.__module__`）归属
——这样 `config/teams.py` 的 12 个端点算在 teams 名下，而不是混进包一级的 58 个里。
新加一个端点自动进分母，要么带着界面来，要么进 `EXEMPT` 并写明为什么不需要界面。

## 判据（以及它承认的盲区）

匹配的是**源码里的调用形态**（`/api/spd/teams/${id}/members` 这样的模板字符串），
不是运行时行为——免构建前端没有 jest，这里守"入口存在"，行为由后端测试守。
三条明说的乐观与盲区，都在 `test_覆盖面自证` 里打印出来、并各自有数字：

1. **按端点计分母、按路径判命中**：同一路径上的 GET 与 PATCH，只要有人写过
   这条路径就都算命中。能证明"有人碰过这条路径"，不能证明每个动词都有调用点。
2. **路径由变量拼出来的调用看不见**（`authApi(btn.dataset.refDetail)`）：这类
   调用点按"盲区"计数并逐条打印，不假装看得见。数量由 `BLIND_SPOT_CALLS` 钉住。
3. **命中只要求路径字面量出现在静态资源里**，出现在注释里也算数。收紧这一条要
   真的解析 JS，免构建仓库里不值当；上一轮的教训是宁可**如实声明**，也不要
   报一个"扫了 11% 的文件却写着 100% 覆盖率"的数字。

## 棘轮语义（与 test_api_contract_governance 同一手法）

* 总欠账 `BASELINE_ORPHANS`：只许调小。补一处界面就把数字改小。
* `FULLY_COVERED`：已经零缺口的模块，**逐字等于**从结构算出来的现实集合。
  只有总基线的话，"这边补一个、那边掉一个"会完全静默；两个方向都关上之后，
  这份清单等价于从代码推导，人不必再记得什么。

## 与既有守卫的分工（不设第二个同判定的闸门）

* 本文件**取代**了 `test_spd_care_frontend_coverage.py`：那份文件的端点覆盖判据
  是本文件的一个子集（care 一个模块），留着就是两份各判各的。它另外守的
  "居民端咨询入口可达"是调用点扫描看不见的**可达性**，搬到本文件末尾一节。
* "render 函数必须挂进 PAGES"由 `test_frontend_page_registry.py` 泛化守着
  （既没注册、也没人调用的 render 函数当场变红），本文件不再重复判。
* 转义由 `test_frontend_escape_guard.py` 守，渲染取证由 `scripts/render_diff.js` 做。
"""
from __future__ import annotations

import importlib
import pkgutil
import re
import warnings
from pathlib import Path

from fastapi import APIRouter
from fastapi.routing import APIRoute

import app.routers as platform_routers
import app.spd.routers as spd_routers

SERVER = Path(__file__).resolve().parents[1]
STATIC = SERVER / "app" / "static"

#: 扫描的前端资源：`app/static` 下全部 JS 与 HTML（含 `m/` 移动端子目录）。
#: 目录事实即清单——新加一个页面文件自动进扫描面，不需要谁记得来登记。
STATIC_FILES = sorted(STATIC.rglob("*.js")) + sorted(STATIC.rglob("*.html"))

#: 豁免：路径 -> 为什么这个端点**不需要**界面。只许变少，新增须写清理由。
EXEMPT: dict[str, str] = {
    "/api/spd/measurements/batch": (
        "设备/物联网/HIS 批量回传通道（需求一#13/#16 承诺的是**接入路径**而非界面）；"
        "蓝牙血压计不点网页"
    ),
    "/api/integration/hl7v2/patient": "HL7 v2 入站：院内 HIS 推消息进来，机器对机器",
    "/api/integration/hl7v2/adt": "HL7 v2 ADT 入站（A01/A03/A04/A08），机器对机器",
    "/api/integration/hl7v2/oru": "HL7 v2 ORU^R01 检验结果入站，机器对机器",
    "/api/integration/fhir/Patient": "FHIR R4 Patient 入站，机器对机器",
    "/api/integration/fhir/Observation": "FHIR R4 Observation 入站，机器对机器",
    "/api/integration/fhir/DiagnosticReport": "FHIR R4 DiagnosticReport 入站，机器对机器",
    "/api/integration/fhir/Encounter": "FHIR R4 Encounter 入站，机器对机器",
    "/api/integration/fhir/Patient/{ehc_no}": (
        "FHIR R4 患者档案导出，供省平台前置机拉取（与 jobs.fhir_batch_export 同一出口），"
        "机器对机器"
    ),
}

#: 当前没有前端调用点、也没豁免的端点数。**只允许调小。**
#: 轨迹：241（本闸门建成时的实测）→ 232（扣掉 9 条机器对机器豁免）
#: → 220（补上 spd/config/teams 的服务团队与村医配置界面，12 个端点）。
BASELINE_ORPHANS = 199

#: 路径由变量拼出来、扫描看不见的调用点。**只允许调小。**
#: 这不是欠账，是闸门的视野边界——如实登记，不假装看得见。
BLIND_SPOT_CALLS = 9

#: 零缺口模块：这些模块的每个端点都有调用点（或书面豁免），**不许回退**。
#: 由 `test_零缺口模块清单不许落后现实` 钉住它与结构推导的结果逐字相等。
FULLY_COVERED = frozenset({
    "routers.access_logs",
    "routers.accounting",
    "routers.analytics",
    "routers.attachments",
    "routers.auth",
    "routers.blood",
    "routers.clinical_docs",
    "routers.contracts",
    "routers.cost",
    "routers.dataquality",
    "routers.dispense",
    "routers.encounters",
    "routers.followups",
    "routers.homevisits",
    "routers.insurance",
    "routers.jobs",
    "routers.knowledge",
    "routers.labqc",
    "routers.materials",
    "routers.maternal",
    "routers.medwaste",
    "routers.monitor",
    "routers.notifications",
    "routers.pathology",
    "routers.performance",
    "routers.printing",
    "routers.publichealth",
    "routers.referrals",
    "routers.reports",
    "routers.rules",
    "routers.staffing",
    "routers.surgery",
    "routers.surveys",
    "routers.telemedicine",
    "routers.todos",
    "routers.vaccination",
    "routers.workflows",
    "spd.routers.care",
    "spd.routers.config.teams",
    "spd.routers.workbench",
    # care 是上一轮补齐的（31 个端点从零到全覆盖，豁免只剩批量回传那一条）；
    # config.teams 是本轮补齐的（服务团队与村医配置界面）。
})


# ---------------------------------------------------------------- 分母：路由

#: 本闸门**自己**走到的模块名（`_iter_routes` 边扫边记）。
#: 不查 `sys.modules`：那是进程全局的，别的用例 import 过 `app.main` 之后
#: 整棵路由树都在里面，拿它来核对等于自己给自己打分。
SCANNED_MODULES: set[str] = set()


def _iter_routes():
    """全部路由模块里的 `APIRoute`，按对象身份去重后逐条产出。

    去重是必须的：`spd/config` 是包，`catalog`/`teams`/… 六个子模块都从 `_base`
    导入**同一个** `router` 对象，逐模块扫描会把这 58 个端点数 6 遍。
    """
    seen: dict[int, APIRoute] = {}
    for pkg in (platform_routers, spd_routers):
        for modinfo in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
            module = importlib.import_module(modinfo.name)
            SCANNED_MODULES.add(modinfo.name)
            for router in (v for v in vars(module).values() if isinstance(v, APIRouter)):
                for route in router.routes:
                    if not isinstance(route, APIRoute):
                        continue
                    if not (route.methods - {"HEAD", "OPTIONS"}):
                        continue
                    seen[id(route)] = route
    return list(seen.values())


def _owner(route: APIRoute) -> str:
    """端点归属的模块——按 **handler 定义在哪个文件** 算，不是按挂在谁的 router 上。

    差别是实打实的：`spd/config` 的 58 个端点挂在同一个 router 上，按 router 算
    会糊成一个 key，`teams.py` 的 12 个孤儿就被同包里另外 46 个有界面的端点
    盖过去了——而本轮量出来的缺口正是它。
    """
    return route.endpoint.__module__.removeprefix("app.")


ROUTES = _iter_routes()


# ---------------------------------------------------------------- 判据：调用点

def _static_source() -> str:
    return "\n".join(p.read_text("utf-8") for p in STATIC_FILES)


SOURCE = _static_source()


def _pattern_for(path: str) -> re.Pattern:
    """把路由模板变成"前端怎么写这个调用"的正则。

    `{param}` 在免构建前端里是模板插值 `${...}` 或写死的数字；其余部分按字面匹配。
    参数段**刻意不放宽到任意单词**：放宽了，`/api/spd/tasks/summary` 就会被算作
    `/api/spd/tasks/{task_id}` 的调用点（实测这类假命中有 5 处），闸门就开始
    自己给自己发合格证。

    末尾的否定环视是第二道收紧：没有它，`/api/spd/teams` 会被
    `/api/spd/teams/${id}/members` 顺带算作命中——"子路径有人调"不等于
    "这条路径有人调"（实测这类假命中有 24 处）。
    """
    parts = [
        re.escape(seg) if not seg.startswith("{") else r"(?:\$\{[^}]*\}|\d+)"
        for seg in path.strip("/").split("/")
    ]
    return re.compile("/" + "/".join(parts) + r"(?![\w/.-])")


def _has_call_site(path: str) -> bool:
    return bool(_pattern_for(path).search(SOURCE))


def _orphans() -> list[tuple[str, str, str]]:
    """(模块, 方法, 路径)——没有调用点、也没豁免的端点。"""
    out = []
    for route in ROUTES:
        if route.path in EXEMPT or _has_call_site(route.path):
            continue
        method = sorted(route.methods - {"HEAD", "OPTIONS"})[0]
        out.append((_owner(route), method, route.path))
    return sorted(out)


def _per_module() -> dict[str, list[int]]:
    """模块 -> [端点数, 孤儿数]。"""
    stats: dict[str, list[int]] = {}
    for route in ROUTES:
        row = stats.setdefault(_owner(route), [0, 0])
        row[0] += 1
        if route.path not in EXEMPT and not _has_call_site(route.path):
            row[1] += 1
    return stats


def _fully_covered_in_reality() -> set[str]:
    return {mod for mod, (_total, orphans) in _per_module().items() if orphans == 0}


# ---------------------------------------------------------------- 盲区：变量路径

#: `api(x)` / `authApi(x)` / `fetch(x)`，且首个实参不是以 `/api` 开头的字面量。
#: 排除两类噪声：`function api(path…)` 这样的**定义**，以及注释里的 `api()`。
DYNAMIC_CALL = re.compile(
    r"(?<!function )\b(?:api|authApi|fetch)\(\s*(?![\"'`]/(?:api|m)\b)(?!\))(?P<arg>[^\s)])"
)


def _blind_spot_calls() -> list[str]:
    out = []
    for path in STATIC_FILES:
        src = path.read_text("utf-8")
        for m in DYNAMIC_CALL.finditer(src):
            line = src[: m.start()].count("\n") + 1
            snippet = src[m.start(): m.start() + 70].splitlines()[0]
            out.append(f"{path.relative_to(STATIC)}:{line}  {snippet}")
    return out


# ---------------------------------------------------------------- 自证与棘轮


def test_覆盖面自证():
    """先证明这道闸门**真的扫到了东西**，再谈它守住了什么。

    上一轮的教训："报着 100% 覆盖率、实际只扫了 11% 的文件"。所以这里把
    分母怎么算的、扫了几个文件、命中多少、豁免多少、**认不出的形态有哪些**
    全部打出来，数字对不上当场看得见。
    """
    stats = _per_module()
    total = sum(t for t, _ in stats.values())
    orphans = _orphans()
    exempt_hits = [r.path for r in ROUTES if r.path in EXEMPT]
    shared_paths = {}
    for route in ROUTES:
        shared_paths.setdefault(route.path, 0)
        shared_paths[route.path] += 1
    multi_verb = sum(n for n in shared_paths.values() if n > 1)
    blind = _blind_spot_calls()
    summary = "\n".join([
        "",
        "[孤儿端点棘轮] 覆盖面自证",
        f"  分母：路由模块 {len(stats)} 个 / 端点 {total} 个"
        "（app.routers + app.spd.routers 递归全量，按 handler 所属模块归属，无抽样）",
        f"  前端：静态资源 {len(STATIC_FILES)} 个"
        f"（{', '.join(str(p.relative_to(STATIC)) for p in STATIC_FILES)}）",
        f"  命中：{total - len(orphans) - len(exempt_hits)}"
        f"    豁免：{len(exempt_hits)}（{len(EXEMPT)} 条理由）"
        f"    孤儿：{len(orphans)}（基线 {BASELINE_ORPHANS}）",
        f"  零缺口模块：{len(_fully_covered_in_reality())} / {len(stats)}",
        "  —— 以下是这道闸门**看不见**的部分，如实计数 ——",
        f"  盲区①按路径判命中：{multi_verb} 个端点与别的端点共用路径，"
        "只能证明有人碰过这条路径，不能证明每个动词都有调用点",
        f"  盲区②路径由变量拼出来的调用点：{len(blind)} 处（基线 {BLIND_SPOT_CALLS}）",
        *(f"      {b}" for b in blind),
        "  盲区③命中只要求路径字面量出现在静态资源里（注释里也算）",
    ])
    print(summary)
    warnings.warn(summary, UserWarning, stacklevel=2)

    assert total > 900, f"只扫到 {total} 个端点——仓库现有 900+，推导八成断了"
    assert len(stats) > 80, f"只扫到 {len(stats)} 个路由模块——递归遍历八成断了"
    assert len(STATIC_FILES) >= 12, f"只扫到 {len(STATIC_FILES)} 个前端文件"


def test_扫描面覆盖全部路由源文件():
    """磁盘上的每一个路由源文件都必须被 import 到。

    这条是"自证"的硬化版：`walk_packages` 漏掉一个子包（比如某天 `config` 被
    改成命名空间包），孤儿数会**变小**，闸门看起来更绿——绿得毫无道理。
    以目录事实为准逐个核对，漏一个当场红。
    """
    on_disk = set()
    for pkg_dir in (SERVER / "app" / "routers", SERVER / "app" / "spd" / "routers"):
        for py in pkg_dir.rglob("*.py"):
            if py.name == "__init__.py":
                continue
            on_disk.add(py.relative_to(SERVER).with_suffix("").as_posix().replace("/", "."))
    missing = sorted(on_disk - SCANNED_MODULES)
    assert missing == [], (
        f"以下路由源文件没有被本闸门扫到，它们的端点不在分母里：{missing}"
    )
    assert len(SCANNED_MODULES) >= len(on_disk) > 80


def test_端点都有前端调用点或书面豁免():
    orphans = _orphans()
    detail = "\n  ".join(f"{mod:38} {method:6} {path}" for mod, method, path in orphans)
    assert len(orphans) <= BASELINE_ORPHANS, (
        f"没有前端调用点的端点从基线 {BASELINE_ORPHANS} 涨到 {len(orphans)}。"
        "新端点要么带界面来，要么进 EXEMPT 并写明为什么不需要界面：\n  " + detail
    )
    if len(orphans) < BASELINE_ORPHANS:
        print(f"\n[提示] 孤儿端点已降到 {len(orphans)}，请把 BASELINE_ORPHANS "
              f"从 {BASELINE_ORPHANS} 下调到 {len(orphans)}。")


def test_零缺口模块不许回退():
    stats = _per_module()
    regressed = {m: stats.get(m, [0, 0])[1] for m in FULLY_COVERED if stats.get(m, [0, 0])[1]}
    assert not regressed, (
        f"以下模块已登记为零缺口，却出现了没有调用点的端点（回退）：{regressed}。"
        " 删界面前先把端点也删掉，或者说明为什么它不再需要界面。"
    )
    # 模块整个消失（改名/拆包/搬家）时，上面那条按 [0, 0] 兜底**恒绿**——清单会
    # 悄悄变成一份没人看得懂的化石，而它守的那些端点已经换了名字、不再受保护。
    vanished = sorted(m for m in FULLY_COVERED if stats.get(m, [0, 0])[0] == 0)
    assert vanished == [], (
        f"以下登记为零缺口的模块已经不存在（或不再有端点）：{vanished}。"
        " 拆包/改名时把清单一起改——否则它守的端点换了个名字就没人守了。"
    )


def test_零缺口模块清单不许落后现实():
    """`FULLY_COVERED` 必须**逐字等于**结构推导出来的零缺口模块集合。

    少登记：该模块日后掉一个调用点时不会单独变红，只要总欠账没顶破基线就静默
    过去（"这边补一个、那边掉一个"正好抵消）。多登记：由上一条报出来。
    两条合起来，这份清单不需要谁记得维护。
    """
    reality = _fully_covered_in_reality()
    missing = sorted(reality - FULLY_COVERED)
    assert missing == [], (
        f"以下模块已经零缺口，但没登记进 FULLY_COVERED：{missing}。"
        " 不登记 = 它日后掉了调用点不会单独变红（总欠账一增一减就掩盖过去了）。"
    )


def test_豁免只许变少且不许指向不存在的端点():
    assert len(EXEMPT) <= 9, (
        f"孤儿端点豁免应保持 ≤9 项（现 {len(EXEMPT)}）；"
        "新增豁免须写明为什么该端点不需要界面，且总数只许变少"
    )
    live = {route.path for route in ROUTES}
    stale = sorted(p for p in EXEMPT if p not in live)
    assert stale == [], (
        f"以下豁免指向的端点已经不存在了：{stale}。删端点时顺手把豁免也删掉，"
        "否则豁免清单会慢慢变成一份没人看得懂的历史遗迹。"
    )
    for path, reason in EXEMPT.items():
        assert len(reason) >= 10, f"{path} 的豁免理由太短，写清楚为什么不需要界面"


def test_盲区不许变大():
    """路径由变量拼出来的调用点——闸门看不见的那部分，只许变少。

    现存的 9 处是三类：三套前端各自的 `api()`/`authApi()` 包装器（它们的实参
    当然是形参）、包装器内部转发给 `fetch(path…)`，以及 `m/m.js` 里一处
    **路径整个来自后端数据**的调用（`authApi(btn.dataset.refDetail)`）。
    新写的调用一律用字面量路径——拼出来的路径，这道闸门与任何 grep 都看不见。
    """
    blind = _blind_spot_calls()
    assert len(blind) <= BLIND_SPOT_CALLS, (
        f"看不见路径的调用点从 {BLIND_SPOT_CALLS} 涨到 {len(blind)}：\n  "
        + "\n  ".join(blind)
    )


# ---------------------------------------------------------------- 反空转自检


def test_判据不空转_假路径必须判成孤儿():
    """一条谁也没调过的路径必须判成孤儿；判成命中说明正则被写松了。"""
    assert not _has_call_site("/api/this-endpoint-does-not-exist/{oid}/nowhere")


def test_判据不空转_真调用点必须判成命中():
    """挑三条形态不同的真调用：无参数的、带 `${}` 插值的、带写死数字的。"""
    assert _has_call_site("/api/spd/teams")                       # 列表
    assert _has_call_site("/api/spd/teams/{team_id}/members")     # 模板插值
    assert _has_call_site("/api/auth/login")                      # 登录


def test_判据不空转_子路径不算父路径的调用点():
    """`/api/xxx/${id}/members` 不该把 `/api/xxx` 也算成命中——末尾环视守这条。"""
    pattern = _pattern_for("/api/spd/teams")
    assert not pattern.search("api(`/api/spd/teams/${id}/members`)")
    assert pattern.search('api("/api/spd/teams?limit=100")')


def test_判据不空转_参数段不吃掉兄弟路径():
    """`/api/spd/tasks/summary` 不该算作 `/api/spd/tasks/{task_id}` 的调用点。"""
    pattern = _pattern_for("/api/spd/tasks/{task_id}")
    assert not pattern.search('api("/api/spd/tasks/summary")')
    assert pattern.search("api(`/api/spd/tasks/${t.id}`)")


# ---------------------------------------------------------------- 可达性
#
# 调用点扫描看不见"入口挂没挂上"：函数写了、接口也调了，按钮没挂上去照样点不进。
# 上一轮村医绑定二维码指向一个 404 地址的教训——别只验代码存在，验**可达**。
# 管理端的"render 函数必须有入口"由 test_frontend_page_registry.py 泛化守着；
# 居民/医生移动端没有注册表，只能逐条钉，下面两条来自已被本文件取代的
# test_spd_care_frontend_coverage.py。


def test_居民端咨询三端点都接上了():
    """患者移动端 #18：发起/列表/消息。这三条断掉，医护侧的应答界面就是空转。"""
    js = (STATIC / "m" / "m.js").read_text("utf-8")
    assert "/api/portal/spd/consults" in js, "居民端缺 /api/portal/spd/consults 的调用"
    assert re.search(r"/api/portal/spd/consults/(\$\{[^}]*\}|\d+)/messages", js), (
        "居民端缺消息列表调用——看不到医生回复的咨询是单向喊话"
    )


def test_居民端咨询入口挂在页签上():
    html = (STATIC / "m" / "index.html").read_text("utf-8")
    assert 'data-spd="consult"' in html, "m/index.html 缺咨询页签按钮"
    js = (STATIC / "m" / "m.js").read_text("utf-8")
    assert 'activeSpd === "consult"' in js, "loadSpd 没接 consult 分支"
