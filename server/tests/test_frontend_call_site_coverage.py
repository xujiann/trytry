"""打印单据 / 医废追溯 / 双因素 / 调阅留痕：入口**挂在哪、怎么挂**。

本文件原本还守着"这四个模块的端点必须有前端调用点"。那条判定已经整条
让给了 `tests/test_frontend_endpoint_coverage.py`——它守的是同一件事，
但分母是全平台 92 个路由模块、945 个端点，而本文件只有四个模块；判据也更紧
（参数段不吃掉兄弟路径、子路径不算父路径的调用点）。**同一条判定留两份守卫，
正是这个仓库上一轮花整轮消灭的形状**，所以留强的那份，删这份。
那四个模块现已登记在对方的 `FULLY_COVERED` 里，掉一个调用点会单独变红。

本文件留下的五条是对方**看不见**的判定——它只回答"有没有人调用过这条路径"，
不回答下面这些：

* **挂在哪**：单据要挂在办这件事的页面上，不是堆一个"打印中心"。住院费用清单
  落到费用结算页、接种证明落到住院页，都属于点得到但没人找得到——全平台棘轮
  照样算命中。
* **怎么挂**：打印一律走 `openPrintPage()`（带令牌取回再唤起打印）。裸
  `window.open` 同样是一个"调用点"，但它会把需要鉴权的单据打成一个 401 页面。
* **给谁挂**：账号安全页**刻意不限角色**。三个 TOTP 端点只认 `get_current_user`，
  把页面挂进仅管理员可见的"用户管理"，会把 director / 医师 / 药师全挡在门外——
  而 `MEDPLAT_TOTP_REQUIRED_ROLES` 里最常填的就是 director。
* **挂在哪一端**：`/api/access-logs/mine` 依赖居民令牌（`current_resident_patient`），
  管理端调它必被拒；它只能落在居民端，且只在**本人视角**下渲染——按账户绑定的
  patient_id 过滤，挂到家庭成员档案下就是张冠李戴。

匹配的是**源码里的调用形态**（模板字符串 `/api/print/settlements/${s.id}`），
不是运行时行为——免构建前端没有 jest，这里守"入口的位置与形态"，行为由后端
测试守，"渲染出来非空且转义"由 `scripts/render_diff.js --dump` 取证。
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
