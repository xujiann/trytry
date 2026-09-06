"""ADR-0009 第二步：`panel()` 组件与**第一页**迁移的守卫。

节奏按 ADR 走：组件与手写共存，一次只迁一页、人工过一页。本文件盯两件事——
组件自身的转义边界，以及已迁完的那一页不要退回手写。

顺带记一条方法论结论：**逐页人工过，找到了全量正则扫描找不到的东西。**
上一轮对三套前端做过一次插值扫描（见 test_frontend_chart_escaping.py 的文件注释），
结论是"文本字段类命中 30 条、真实漏洞 0 条"。那个结论**有盲区**：扫描只认
`x.y` 形式的属性访问，而这一页真正没转义的是一个**解构出来的局部变量**
（`const [text, color] = UNIFIED_STATUS[i.status] || [i.status, ""]` ——
查不到映射时 `text` 回落成后端原始状态码，裸插进 innerHTML）。
迁这一页时人眼看出来的。这正是 ADR 说"迁移一页、人工过一页，不要批量改"的理由。

---------------------------------------------------------------------------
第二批（2026-08-26）：又迁六页（随访 / 定时任务 / 满意度 / 站内消息 /
质量指标 / 运行监控），`pages-mgmt.js` 里 16 处手写外壳换成 `panel()`。

这一批的三条结论：

1. **迁移本身是 no-op，但要有证据。** 新增了 `scripts/render_diff.js`：在 Node 里
   把页面按夹具真渲染出来，拿迁移前后的 innerHTML 逐字符比。六页全部一致。
   上一轮的比对脚本是一次性的，这次做成可复用的——"逐页迁、不设期限"意味着
   后面还有 ~300 处外壳，每页重写一遍取证脚本不合算。

2. **标题里的 `esc()` 必须去掉。** `panel()` 自己转义 title，调用方再包一层就是
   转两遍（`&` → `&amp;amp;`），是**改字节**。这不是推演：`renderMonitor` 原样
   保留 `esc(stats.scope)` 时，比对器当场报出
   `本实例进程内&lt;b&gt;` → `本实例进程内&amp;lt;b&amp;gt;`。
   `test_已迁移的页面标题不得重复转义` 盯住这条。

3. **顺着"人工过一页"又挖出一批真问题——而且这次是按形状挖的。**
   上一轮撞见的那个"解构出来的局部变量裸插值"，当时只修了眼前那一处。
   这次把**这个形状**全仓库扫了一遍：同样的写法还有 33 处，散在五个文件里，
   也就是个案修复漏掉了 97%。守卫见 `test_frontend_escape_guard.py`。
   教训写在那个文件的 docstring 里：撞见一处未转义，要立刻扫形状，别只修个案。
"""
import pathlib
import re

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "app" / "static"
CORE = (STATIC / "core.js").read_text(encoding="utf-8")
MGMT = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
#: 第三批起已迁页面不再只在 pages-mgmt.js 里（clinical / public / spd 各有），按函数名找文件。
SOURCES = {p.name: p.read_text(encoding="utf-8") for p in sorted(STATIC.glob("*.js"))}


def _src_for(page: str) -> str:
    """含 `function <page>(` 定义的那份源码；找不到就报名字，别让 `_fn` 报一个看不懂的 ValueError。"""
    hits = [name for name, text in SOURCES.items() if f"function {page}(" in text]
    assert len(hits) == 1, f"{page} 应恰在一个 static/*.js 里定义，实际：{hits}"
    return SOURCES[hits[0]]


def _fn(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    return src[start: src.index("\n}\n", start)]


def _code(src: str) -> str:
    """去掉 `//` 行注释——注释里的字不是代码。

    数 `panel(` 的调用数时被自己写的迁移注释坑过一次：注释里那句
    "面板外壳改用 `panel()`" 也被 `count("panel(")` 数了进去，`renderMonitor`
    因此报 6 处（实际 4 处调用 + 2 处注释）。`test_frontend_chart_escaping.py`
    的文件注释里记着同一个坑，这里沿用同样的处理。
    """
    return "\n".join(re.sub(r"//.*$", "", line) for line in src.splitlines())


def _panel_titles(src: str):
    """把每个 `panel(` 调用的**第一个实参**切出来。

    初版判据是一条正则 `panel\\(\\s*`[^`]*\\$\\{esc\\(`——它要求 `panel(` 后面
    紧跟一个反引号，于是只认"模板字符串标题"。自审时试出来：标题是**单个动态值**
    时最自然的写法是 `panel(esc(stats.scope), …)`，一个反引号都没有，判据直接漏掉，
    而这恰恰是迁移前 `<h3>接口调用（${esc(stats.scope)}）</h3>` 最容易被改成的样子。
    改成按逗号切实参——判据要认的是"标题里有没有 esc()"，不是"标题怎么引号"。
    """
    titles, i = [], 0
    while (i := src.find("panel(", i)) != -1:
        i += len("panel(")
        depth, start, quote = 0, i, None
        while i < len(src):
            ch = src[i]
            if quote:                                   # 字符串/模板内部不看括号
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    quote = None
            elif ch in "\"'`":
                quote = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    break                               # 只有一个实参
                depth -= 1
            elif ch == "," and depth == 0:
                break                                   # 第一个实参到此为止
            i += 1
        titles.append(src[start:i])
    return titles


# ------------------------------------------------------------ 组件自身
def test_panel转义标题():
    """标题由组件负责转义——这是抽组件的**全部理由**（CLAUDE.md §8）。"""
    body = _fn(CORE, "panel")
    assert "${esc(title)}" in body, "panel 没有转义 title，组件就白抽了"


def test_panel的accent也转义():
    """`accent` 插进 style 属性，同样不能裸插。"""
    assert "esc(accent)" in _fn(CORE, "panel")


def test_panel刻意不转义body_且这条边界写在注释里():
    """`body` 是调用方拼好的 HTML，组件不能替它转义。

    这不是疏漏，是必须讲清楚的边界——否则"用了组件就安全了"会变成错觉。
    要求注释里明确写出来，免得后来人以为组件包办了全部转义。
    """
    body = _fn(CORE, "panel")
    assert "${body}" in body, "body 应原样插入"
    doc = CORE[CORE.index("/**", CORE.index("面板外壳") - 400): CORE.index("function panel(")]
    assert "不转" in doc or "不能替它转义" in doc, "组件的转义边界没有写在文档注释里"


# ------------------------------------------------------------ 已迁移的页面
#: 迁一页就往这里加一条（页面名 → 该页的 panel() 数量）。
#: 组件与手写共存是 ADR 定的节奏，所以这里是**白名单**而不是"全文件不许有手写外壳"。
MIGRATED_PAGES = {
    "renderServiceRequests": 2,     # 2026-08-22 第一页
    "renderFollowups": 3,           # 以下六页 2026-08-26
    "renderJobs": 2,
    "renderSurveys": 4,
    "renderNotifications": 1,
    "renderClinicalIndicators": 2,
    "renderMonitor": 4,
    # 第三批 2026-09-02：挑的是 render_diff 夹具**已经有**的五页——比对取证零成本，
    # 也是第一次迁出 pages-mgmt.js（clinical / public / spd 各一到两页）
    "renderMaterials": 4,
    "renderMedication": 4,          # 含一处带 accent 的条件面板（供应风险）
    "renderBilling": 5,
    "renderEsb": 4,
    "renderSpdCenter": 5,
    # 第四批 2026-09-02：先给五页补夹具（team / hc / oaqc / infectious / blood 各带 XSS 载荷）再迁；
    # 条件面板（预警 / 服务包 / 按角色显示的表单）条件仍留在调用点
    "renderSpdTeam": 6,
    "renderSpdHealthCommission": 7,
    "renderOaQc": 4,
    "renderInfectious": 3,
    "renderBlood": 4,
    # 第五批 2026-09-02：接种（渲染时不调接口，夹具 api 为空）/ 名老中医医案 / DRGs / 科室成本
    "renderVaccination": 4,
    "renderTcmHeritage": 4,
    "renderDrgs": 4,
    "renderCost": 5,                # 标题里的期间不再 esc()：组件转义标题
    # 第六批 2026-09-02：慢专病运行中枢 / 症候群病原监测 / 通用资源 / 医保
    "renderSpdAdmin": 5,
    "renderSurveillance": 4,
    "renderResources": 4,
    "renderInsurance": 4,
    # 第七批 2026-09-06：知识库 / 人力财务物资 / 医疗质量 / 慢专病考核 / 慢专病随访。
    # 五页都是先补 render_fixtures.json 条目（迁前比一次证明表格真渲染出来了：
    # 2486～7057 字符），迁完再比一次仍逐字符相同，全部 31 个夹具页一并重跑无一差异。
    "renderKnowledge": 3,           # 含一处带橙色左边框的临期提醒（走 accent）
    "renderHrFinance": 8,           # 另有两处 `class="panel hidden" id=…` 的容器面板不迁（规则也数不到，见下）
    "renderQuality": 6,
    "renderSpdAssess": 5,           # 第 6 处外壳在点击后的下钻明细里，比对器到不了，见下
    "renderSpdFollowup": 5,
    # 这两页本来就是 panel() 写的（不是本批迁的），但一直没进白名单——
    # 没登记就等于没有"不许退回去"的网，顺手补上。
    "renderSpdMember": 5,
    "renderSpdManager": 3,
    # 第八批 2026-09-06：pages-mgmt.js 四页（住院文书 / 手术麻醉 / 流程引擎 / 运营分析）。
    # 同批的 renderAccounting **没迁**：它的合并报表面板嵌在凭证表格的行模板里，
    # 一条凭证渲染一份（夹具两条凭证 → 出现两次），迁移会把这个结构固化得更难看清；
    # 缺陷已登记（TECH_DEBT P1-41），修完再迁。
    "renderClinicalDocs": 5,
    "renderSurgery": 5,             # 另有一处 class="panel hidden" 的术中记录容器
    "renderWorkflows": 5,           # 另有一处 class="panel hidden" 的流转记录容器
    "renderAnalytics": 5,           # 标题里的期间不再 esc()：组件转义标题
}

#: 已迁移的页面里**故意留下**的手写外壳（页面名 → 条数 → 为什么留）。
#:
#: 迁移的取证方式是 `scripts/render_diff.js`：喂夹具、真渲染、逐字符比。它只能覆盖
#: **渲染路径**——点击之后才产生的 HTML、以及 `panel()` 表达不出来的外壳，都证明不了
#: "换完字节没变"。没有证据就不换，是这套流程的前提，不是偷懒。
#:
#: 注意这里数的是 `<div class="panel"`（`panel` 后面紧跟引号）。`class="panel hidden"`
#: 那种带附加 class 的容器**这条规则本来就看不见**——`renderHrFinance` 里就有两个
#: （先渲染成 hidden、点击后才填内容），它们同样迁不了（panel() 出不了额外 class 与 id），
#: 只是不必登记在这里。别把"计数为 0"读成"这页一个手写外壳都没有了"。
KNOWN_UNMIGRATED_SHELLS = {
    # 点"下钻明细"之后才写进 #spd-score-detail 的带 accent 面板。标题里现在是
    # `${esc(d.object_name)}`，换成 panel() 要把 esc 去掉交给组件——**通常**等价，
    # 但若后端把 total_score 之类的数值字段返回成带 & 的字符串，两种写法的字节就不同了。
    # 比对器点不到这条路径，也就证不了这一步，所以留着等取证工具能覆盖点击路径再说。
    "renderSpdAssess": (1, "点击后才渲染的下钻明细面板，render_diff 覆盖不到"),
}
MIGRATED = "renderServiceRequests"


@pytest.mark.parametrize("page,count", sorted(MIGRATED_PAGES.items()))
def test_已迁移的页面不再手写panel外壳(page, count):
    fn = _code(_fn(_src_for(page), page))
    allowed, why = KNOWN_UNMIGRATED_SHELLS.get(page, (0, ""))
    shells = fn.count('<div class="panel"')
    assert shells == allowed, (
        f"{page} 的手写 panel 外壳应为 {allowed} 处"
        + (f"（{why}）" if why else "——迁过的页面不要退回去")
        + f"，实际 {shells} 处"
    )
    assert fn.count("panel(") == count, (
        f"{page} 应当有 {count} 处 panel() 调用，实际 {fn.count('panel(')} 处"
    )


@pytest.mark.parametrize("page", sorted(MIGRATED_PAGES))
def test_已迁移的页面标题不得重复转义(page):
    """`panel()` 自己转义 title，调用方再包一层 `esc()` 就是转两遍。

    这不是洁癖：`&` 会变成 `&amp;amp;`、`<` 变成 `&amp;lt;`，页面上直接显示出
    转义序列——是**改字节**。迁 `renderMonitor` 时原样保留 `esc(stats.scope)`
    当场被渲染比对器抓到（`本实例进程内&lt;b&gt;` → `本实例进程内&amp;lt;b&amp;gt;`）。
    """
    for title in _panel_titles(_code(_fn(_src_for(page), page))):
        assert "esc(" not in title, (
            f"{page} 的 panel() 标题 `{title.strip()[:60]}` 里还留着 esc()——"
            f"组件已经转义过一次了，留着就是转两遍（改字节）"
        )


def test_统一状态列走组件而不是手写():
    """`UNIFIED_STATUS` 查不到时回落成后端原始状态码——那是服务端数据。

    这条原本钉的是"手写的那一处有没有 `esc()`"（2026-08-22 修掉的那个裸插值）。
    现在这一列已收敛到 `statusTag()`（P2-26），判据随之**变强**：不再是
    "这一处记得转义了吗"，而是"这一处根本不自己拼 HTML"——转义由组件负责，
    调用点连忘的机会都没有。组件自身的转义由
    `test_frontend_shared_utils.py::test_statusTag把文案与配色都转义了` 盯着。
    """
    fn = _code(_fn(MGMT, MIGRATED))
    assert "statusTag(UNIFIED_STATUS, i.status)" in fn, (
        "统一状态列没走 statusTag()——退回手写就等于把转义责任又交回给人"
    )
    assert 'class="tag ${color}"' not in fn, "又出现了手写的状态标签"
    assert "${text}</span>" not in fn, "还留着裸插的 ${text}"


@pytest.mark.parametrize("marker", ["ADR-0009", "迁一页"])
def test_迁移留了路标给下一页(marker):
    """下一个人得看得出"这页迁过了、按这个套路迁下一页"。"""
    assert marker in _fn(MGMT, MIGRATED), f"迁移注释里缺少 {marker}"


def test_守卫本身没瞎():
    """`_fn` 取错范围时，上面几条会在空字符串或超长字符串上失去区分力。"""
    fn = _fn(MGMT, MIGRATED)
    assert 500 < len(fn) < 4000, f"{MIGRATED} 取到 {len(fn)} 字符，函数体范围不对"
    assert "sr-form" in fn, "取到的不是这个函数"
    assert len(_fn(CORE, "panel")) < 400, "panel 函数体取得过长，范围不对"


@pytest.mark.parametrize(
    "label,call",
    [
        # 初版判据要求 `panel(` 后紧跟反引号，只认模板字符串标题。
        # 标题是**单个动态值**时最自然的写法没有反引号，直接漏掉——而它恰恰是
        # 迁移前 `<h3>接口调用（${esc(stats.scope)}）</h3>` 最容易被改成的样子。
        ("裸 esc() 标题", "panel(esc(stats.scope), `body`)"),
        ("单引号拼接标题", "panel('接口调用（' + esc(s.scope) + '）', `body`)"),
        ("模板字符串标题", "panel(`接口调用（${esc(s.scope)}）`, `body`)"),
    ],
)
def test_重复转义判据不得被标题的写法绕开(label, call):
    titles = _panel_titles(call)
    assert titles and any("esc(" in t for t in titles), (
        f"「{label}」这种写法绕过了「标题不得重复转义」的判据"
    )


def test_标题切分不会把body也算进来():
    """防误报：`body` 里出现 esc() 是**正常且必须**的（组件不转义 body）。

    切分要是把整个调用都当成标题，这条守卫会把每一个用了 esc() 的页面都报红，
    最后只能被删掉或加豁免。
    """
    titles = _panel_titles('panel("运行环境", `<b>${esc(ov.instance_id)}</b>`)')
    assert titles == ['"运行环境"'], f"标题切分越界了：{titles}"
