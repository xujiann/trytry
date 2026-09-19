"""打印单据 / 医废追溯 / 双因素 / 调阅留痕：每个端点都必须有前端调用点。

**这是补齐四处"后端交付了、界面缺失"之后立的桩**，形状与
`test_spd_care_frontend_coverage.py`（2026-08-27 补 spd/care 31 个孤儿端点时立的）
完全一致——同一个毛病在平台侧还有四处：

* `printing`：八类单据（住院费用清单、结算单、病案首页、体检报告、知情同意书、
  疫苗接种证明、转诊单、出院小结）服务端版式都渲染好了，没有一个页面点得到；
* `medwaste`：点位管理（增/查/停用/启用）、入暂存、扫码追溯、转运人工作量，
  整整七条路径没有界面——而"医废点位管理/转运人员管理/医废追溯"正是需求点名的三项；
* `auth`：TOTP 三个端点做完了，工作人员没有任何绑定入口，被要求双因素的角色
  登录时只收到一句 `totp_setup_required`，然后无处可去；
* `access_logs`：`/mine`（居民自己看"谁看过我"，《个保法》第 44 条）与 `/stats`
  （按依据的构成比）只有后端。

孤儿端点的坏处有两面：使用者以为功能不存在（有的还被需求对照表算作已实现），
攻击者拿到的却是一片没人走过的接口面。

**分母从路由对象现算**，不抄清单：四个路由每加一个端点，本用例自动把它计入，
新端点要么带着界面来、要么进 `EXEMPT` 并写明为什么不需要界面。豁免只许变少。

匹配的是**源码里的调用形态**（模板字符串 `/api/print/settlements/${s.id}`），
不是运行时行为——免构建前端没有 jest，这里守"入口存在"，行为由后端测试守，
"渲染出来非空且转义"由 `scripts/render_diff.js --dump` 取证（见 PR 说明）。
"""
import re
from pathlib import Path

from app.routers import access_logs, auth, medwaste, printing

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
#: 三端的全部 JS：管理端 + 居民/村医移动端
JS_FILES = sorted(STATIC.glob("*.js")) + sorted((STATIC / "m").glob("*.js"))

#: 受本用例约束的四个路由（模块名 -> 路由对象）
ROUTERS = {
    "printing": printing.router,
    "medwaste": medwaste.router,
    "auth": auth.router,
    "access_logs": access_logs.router,
}

#: 豁免：path -> 为什么这个端点**不需要**界面。只许变少，新增须写清理由。
EXEMPT: dict[str, str] = {}


def _all_js() -> str:
    return "\n".join(p.read_text("utf-8") for p in JS_FILES)


def _pattern_for(path: str) -> re.Pattern:
    """把路由模板变成"前端怎么写这个调用"的正则。

    `{param}` 在免构建前端里是模板插值 `${...}` 或写死的数字；其余按字面匹配。
    只认路径本身，不管查询串与动词。
    """
    parts = [re.escape(seg) if not seg.startswith("{") else r"(\$\{[^}]*\}|\d+)"
             for seg in path.strip("/").split("/")]
    return re.compile("/" + "/".join(parts))


def _route_paths(router) -> list[str]:
    return sorted({route.path for route in router.routes if hasattr(route, "methods")})


def test_四个模块的端点都有前端调用点或书面豁免():
    js = _all_js()
    orphans = []
    for module, router in ROUTERS.items():
        for path in _route_paths(router):
            if path in EXEMPT:
                continue
            if not _pattern_for(path).search(js):
                orphans.append(f"{module}: {path}")
    assert not orphans, (
        "以下端点没有任何前端调用点（新端点要么带界面、要么进 EXEMPT 并写明理由）：\n  "
        + "\n  ".join(orphans)
    )


def _js(name: str) -> str:
    return (STATIC / name).read_text("utf-8")


def test_八类单据的打印入口挂在对应业务页():
    """单据要挂在**办这件事的页面**上，不是堆一个"打印中心"。

    验的是"哪个文件里有这个调用"而不只是"全仓有"：住院费用清单落到费用结算页、
    接种证明落到住院页，都属于点得到但没人找得到。
    """
    expected = {
        "pages-clinical.js": [
            "/api/print/inpatient-bills/",    # 住院管理页 · 住院记录行
            "/api/print/case-summaries/",     # 住院管理页 · 病案首页
            "/api/print/discharge-summaries/",  # 住院管理页 · 已出院才给入口
            "/api/print/settlements/",        # 费用结算页 · 结算单
            "/api/print/consents/",           # 知情同意页 · 同意书台账
            "/api/print/vaccinations/",       # 疫苗接种页 · 接种史每一行
        ],
        "pages-public.js": ["/api/print/checkups/"],   # 证明与体检页 · 体检记录
        "core.js": ["/api/print/referrals/"],          # 双向转诊页 · 转诊单
    }
    missing = [f"{f}: {path}" for f, paths in expected.items()
               for path in paths if path not in _js(f)]
    assert missing == [], "以下单据的打印入口不在预期的业务页里：\n  " + "\n  ".join(missing)


def test_打印入口都走统一的openPrintPage():
    """打印页要带令牌取回再写进新窗口（服务端已按可见性判定并留痕）。

    直接 `window.open("/api/print/...")` 会走一次**不带 Authorization 头**的
    裸导航，Cookie 模式下侥幸能开、Header 模式下必 401——两种模式各中一半，
    正是最难查的那种。统一收在 `openPrintPage()` 里就不会写岔。
    """
    offenders = []
    for path in JS_FILES:
        for lineno, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            # `/api/print/templates` 是抬头/页脚的 JSON 配置接口，不是单据，走 api()
            if "/api/print/templates" in line:
                continue
            if "/api/print/" in line and "openPrintPage" not in line and "//" not in line:
                offenders.append(f"{path.name}:{lineno}: {line.strip()[:100]}")
    assert offenders == [], (
        "以下地方直接引用了打印端点却没走 openPrintPage()：\n  " + "\n  ".join(offenders))


def test_医废点位与追溯的入口都在医废页():
    """点位的增/查/停用/启用、入暂存、扫码追溯、转运人工作量——七条一条都不能少。

    后端 docstring 把"点位停用不影响历史记录"写成口径，界面就必须是**停用/启用**
    而不是删除：`DELETE /locations/{id}` 实际只置 active=false。
    """
    js = _js("core.js")
    for call in ('api("/api/medwaste/locations"',            # 建档
                 '/api/medwaste/locations?include_inactive=true',  # 含停用的清单
                 "/api/medwaste/locations/${locoff}",        # 停用
                 "/api/medwaste/locations/${locon}/reactivate",    # 启用
                 "/api/medwaste/${store}/store",             # 入暂存
                 "/api/medwaste/trace/",                     # 扫码追溯
                 '/api/medwaste/handler-stats'):             # 转运人工作量
        assert call in js, f"医废页缺 {call} 的调用点"
    assert "data-locoff=" in js and "data-locon=" in js, (
        "点位停用/启用没有按钮——写了调用却没有入口，等于没补")


def test_双因素页已注册进导航且不限角色():
    """TOTP 是"本人给自己账号加第二把锁"，三个端点也只认 get_current_user。

    挂进仅管理员可见的页面，等于把 director/医师/药师全挡在门外——而
    `MEDPLAT_TOTP_REQUIRED_ROLES` 里最常填的恰恰是 director。
    """
    app_js = _js("app.js")
    entry = [ln for ln in app_js.splitlines() if "renderAccountSecurity" in ln]
    assert entry, "renderAccountSecurity 没注册进 PAGES——页面写了但没有入口"
    assert "roles:" not in entry[0], (
        f"账号安全页不该限角色（现为 {entry[0].strip()}）：限了角色，被要求双因素的"
        "非管理员账号就永远绑不上动态口令")
    mgmt = _js("pages-mgmt.js")
    for path in ("/api/auth/totp/setup", "/api/auth/totp/activate", "/api/auth/totp/disable"):
        assert path in mgmt, f"账号安全页缺 {path} 的调用"


def test_调阅留痕两个视角都接上了():
    """`/stats` 给监管（管理端），`/mine` 给本人（居民端）——两个视角不能互相替代。

    居民端那一段还必须**锁在本人视角里**：`/mine` 按账户绑定的 patient_id 过滤
    （绕不开），切到家庭成员时它返回的仍是本人的记录，挂在成员档案下就是张冠李戴。
    所以这里不是"文件里出现过 viewingPatientId"就算数——那个判据是空的
    （`svcQuery()` 里本来就有一处），得验"就在这一段的上文里"。
    """
    assert "/api/access-logs/stats" in _js("pages-clinical.js"), "管理端缺调阅构成统计"
    m_js = (STATIC / "m" / "m.js").read_text("utf-8")
    assert "/api/access-logs/mine" in m_js, "居民端缺「谁看过我的档案」"
    archive = m_js[m_js.index("async function loadArchive"):m_js.index("async function loadMyAccessLogs")]
    hits = [h.start() for h in re.finditer(r"access-log-list|loadMyAccessLogs\(\)", archive)]
    assert hits, "loadArchive 里找不到「谁看过我的档案」这一段"
    ungated = [archive[max(0, h - 60):h + 40].strip() for h in hits
               if "viewingPatientId === null" not in archive[max(0, h - 200):h]]
    assert ungated == [], (
        "「谁看过我的档案」这一段没有锁在本人视角（viewingPatientId === null）里：\n  "
        + "\n  ".join(ungated))


def test_豁免只许变少():
    assert len(EXEMPT) == 0, (
        f"本轮补齐后四个模块零豁免（现 {len(EXEMPT)} 项）；"
        "新增豁免须写明为什么该端点不需要界面，且总数只许变少")


def test_分母确实来自路由对象():
    """反空转自检：路径数对不上说明路由结构变了（拆包/改前缀），上面的推导可能
    整个失效——先修推导再动豁免。数字随端点增删同步更新即可。"""
    actual = {name: len(_route_paths(r)) for name, r in ROUTERS.items()}
    assert actual == {"printing": 13, "medwaste": 9, "auth": 5, "access_logs": 3}, (
        f"四个路由的不同路径数变成了 {actual}；若有意增删端点，同步这里的数字，"
        "并确认新端点带了界面入口")
