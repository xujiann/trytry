"""名老中医医案：四诊、按语录得进看得见，按老师 / 病名 / 证型检索有入口；适宜技术表列出操作要点（P2-1408，第四十一批扫描 AE4-4）。

模块口径是四诊、辨证、治法、处方、按语各存一列——否则检索不出「某某老师治痹证常用哪几味药」；检索接口一直收
`master_name` / `disease` / `syndrome` / `keyword` 四个参数，出参也一直带 `four_exams` / `commentary` / `visit_date` /
`successor_name`。修前页面：录入表单的四诊摘要与按语是单行输入框；医案表只列名老中医、标题、病/证、治法、处方、状态，
前端里 `four_exams` / `commentary` 只出现在录入表单上；检索只送 `keyword`（只搜处方、按语、标题）——搜「痹证」「陈老」
都是空表；按按语里的「附子」检索命中了，结果行的处方列写「乌头汤加减」，看不出为什么命中、也读不到那段按语。
适宜技术同一个形状：`description`（操作要点）入参、出参、入库表单都有，表格只列名称、分类、适应症。

修后（接口不改）：医案行带「展开」，展开一行写就诊日期、传承人、四诊摘要、按语（一律 `esc()`，多行照录入的换行显示）；
四诊摘要与按语改成多行文本框；检索加老师、病名、证型三个筛选，参数名照接口；适宜技术表加「操作要点」列。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
CLINICAL = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
SHARED = (STATIC / "shared.js").read_text(encoding="utf-8")
CORE = (STATIC / "core.js").read_text(encoding="utf-8")
#: 按语里夹一段标签：展开后必须原样转义显示，不能进 DOM
PAYLOAD = "<img src=x onerror=alert(1)>"
#: node 里没有 DOM：`$` 走 document.querySelector，由各用例按选择器喂桩
PRELUDE = ("globalThis.document = { addEventListener() {}, querySelector(sel) { return (globalThis.ELEMENTS || {})[sel] ?? null; },"
           " cookie: '' };\n")

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")


def _top_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    return source[start:source.index("\n}\n", start) + 2]


def _heritage() -> str:
    start = CLINICAL.index("async function renderTcmHeritage()")
    return CLINICAL[start:CLINICAL.index("\nfunction renderCaseTable(", start)]


def _handler(marker: str) -> str:
    """`$("#mc-…").onxxx = … => {` 起到它的 `};` 止（处理函数在渲染器里缩进两格）。"""
    body = _heritage()
    start = body.index(marker)
    return body[start:body.index("\n  };", start) + len("\n  };")]


def _node(script: str, *args: str) -> str:
    done = subprocess.run(["node", "-e", PRELUDE + SHARED + "\n" + script, *args],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.fixture(scope="module")
def cases(client, admin):
    """两条医案：陈老治痹证（四诊、按语多行，按语夹一段标签）、李老治胃痛（只填了必填与病证，按语与就诊日期都空着）。"""
    out = {}
    for key, payload in (
        ("chen", {"master_name": "P21408陈老", "successor_name": "P21408王医生", "title": "P21408 膝关节冷痛案",
                  "disease": "P21408痹证", "syndrome": "寒湿痹阻", "four_exams": "膝冷痛遇寒加重\n舌淡苔白腻，脉沉紧",
                  "treatment_method": "温经散寒", "prescription": "乌头汤加减",
                  "commentary": f"附子先煎一小时\n量自10g渐加，口麻即止{PAYLOAD}", "visit_date": "2026-09-01"}),
        ("li", {"master_name": "P21408李老", "title": "P21408 胃脘痛案", "disease": "P21408胃痛", "syndrome": "P21408脾胃虚寒"}),
    ):
        resp = client.post("/api/tcm-heritage/master-cases", headers=admin, json=payload)
        assert resp.status_code == 201, resp.text
        out[key] = resp.json()
    return out


def _search_path(fields: list[tuple[str, str]]) -> str:
    """把页面的检索处理函数原样拿到 node 里跑：表单按 DOM 次序给出四个框的值，回它请求的地址。"""
    script = (
        "globalThis.ELEMENTS = { '#mc-list': { innerHTML: '' } };\n"
        "globalThis.FormData = class { constructor(form) { this.form = form; }"
        " entries() { return this.form.fields[Symbol.iterator](); } };\n"
        "let requested = null;\n"
        "async function api(path) { requested = path; return []; }\n"
        "function renderCaseTable() { return ''; }\n"
        "const handlers = {};\n"
        + _handler('$("#mc-search").onsubmit = async (e) => {').replace('$("#mc-search").onsubmit', "handlers.search")
        + "\n(async () => {\n"
        "  await handlers.search({ preventDefault() {}, target: { fields: JSON.parse(process.argv[1]) } });\n"
        "  process.stdout.write(requested);\n})();\n"
    )
    return _node(script, json.dumps(fields, ensure_ascii=False))


@needs_node
def test_检索按老师病名证型筛_参数名照接口_留空的不送(client, admin, cases):
    path = _search_path([("master_name", "P21408陈老"), ("disease", "P21408痹证"), ("syndrome", ""), ("keyword", "")])
    assert path == ("/api/tcm-heritage/master-cases?include_draft=true"
                    "&master_name=P21408%E9%99%88%E8%80%81&disease=P21408%E7%97%B9%E8%AF%81")
    got = client.get(path, headers=admin)
    assert got.status_code == 200, got.text
    assert [c["id"] for c in got.json()] == [cases["chen"]["id"]]
    by_syndrome = _search_path([("master_name", ""), ("disease", ""), ("syndrome", "P21408脾胃虚寒"), ("keyword", "")])
    assert [c["id"] for c in client.get(by_syndrome, headers=admin).json()] == [cases["li"]["id"]]
    # 修前页面只送 keyword：keyword 只搜处方、按语、标题，按病名、老师搜都是空表
    assert client.get("/api/tcm-heritage/master-cases", headers=admin,
                      params={"include_draft": "true", "keyword": "P21408痹证"}).json() == []
    keyword_only = _search_path([("master_name", ""), ("disease", ""), ("syndrome", ""), ("keyword", "附子")])
    assert keyword_only == "/api/tcm-heritage/master-cases?include_draft=true&keyword=%E9%99%84%E5%AD%90"


def _render_cases(rows: list[dict]) -> str:
    script = (_top_function(CORE, "table") + _top_function(CLINICAL, "renderCaseTable")
              + _top_function(CLINICAL, "masterCaseDetail")
              + "\nprocess.stdout.write(renderCaseTable(JSON.parse(process.argv[1])));\n")
    return _node(script, json.dumps(rows, ensure_ascii=False))


@needs_node
def test_医案行展开看得到就诊日期传承人四诊按语_一律转义(client, admin, cases):
    chen, li = cases["chen"]["id"], cases["li"]["id"]
    rows = [c for c in client.get("/api/tcm-heritage/master-cases", headers=admin, params={"include_draft": "true"}).json()
            if c["id"] in (chen, li)]
    listed = {c["id"]: c for c in rows}
    assert listed[chen]["commentary"].endswith(PAYLOAD) and listed[chen]["four_exams"]   # 出参一直带着，只是页面不画
    html = _render_cases(rows)
    for cid in (chen, li):
        assert f'<button class="btn secondary" data-mcopen="{cid}">展开</button>' in html   # 修前没有展开
        assert f'<tr class="hidden" data-mcdetail="{cid}"><td colspan="7">' in html          # 默认收着
    detail = re.search(rf'data-mcdetail="{chen}">(.*?)</tr>', html, re.S).group(1)
    assert "就诊日期：<span" in detail and ">2026-09-01<" in detail and ">P21408王医生<" in detail
    assert "膝冷痛遇寒加重\n舌淡苔白腻，脉沉紧" in detail                         # 多行原样（pre-wrap 显示换行）
    assert "附子先煎一小时\n量自10g渐加，口麻即止&lt;img src=x onerror=alert(1)&gt;" in detail
    assert PAYLOAD not in html                                                     # 按语里的标签不进 DOM
    empty = re.search(rf'data-mcdetail="{li}">(.*?)</tr>', html, re.S).group(1)
    assert empty.count("white-space:pre-wrap\">—</span>") == 4                    # 没填的写 —


@needs_node
def test_点展开显示这一行_再点收起():
    """展开行紧跟在医案行后面（renderCaseTable 一条医案画两行）：点「展开」切换它的 hidden，按钮字跟着换；不是这一条的展开行不动。"""
    script = (
        "const classes = new Set(['hidden']);\n"
        "const detail = { dataset: { mcdetail: '7' }, classList: {"
        " toggle(c) { classes.has(c) ? classes.delete(c) : classes.add(c); }, contains(c) { return classes.has(c); } } };\n"
        "const row = { nextElementSibling: detail };\n"
        "function postAction() { throw new Error('展开不该发请求'); }\n"
        "const handlers = {};\n"
        + _handler('$("#mc-list").onclick = (e) => {').replace('$("#mc-list").onclick', "handlers.click")
        + "\nconst button = { dataset: { mcopen: '7' }, textContent: '展开', closest: (sel) => (sel === 'tr' ? row : null) };\n"
        "const seen = [];\n"
        "for (let i = 0; i < 2; i++) { handlers.click({ target: button }); seen.push([classes.has('hidden'), button.textContent]); }\n"
        "const stranger = { dataset: { mcopen: '8' }, textContent: '展开', closest: () => row };\n"
        "handlers.click({ target: stranger });\n"
        "seen.push([classes.has('hidden'), stranger.textContent]);\n"
        "process.stdout.write(JSON.stringify(seen));\n"
    )
    assert json.loads(_node(script)) == [[False, "收起"], [True, "展开"], [True, "展开"]]


def test_四诊摘要与按语是多行文本框_检索表单有老师病名证型():
    body = _heritage()
    form = body[body.index('<form id="mc-form">'):body.index("</form>", body.index('<form id="mc-form">'))]
    for name in ("four_exams", "commentary"):
        assert f'<textarea name="{name}" rows="3"' in form   # 修前是单行 <input>
        assert f'<input name="{name}"' not in form
    search = body[body.index('<form class="inline" id="mc-search">'):]
    search = search[:search.index("</form>")]
    assert re.findall(r'<input name="(\w+)"', search) == ["master_name", "disease", "syndrome", "keyword"]   # 修前只有 keyword
    assert "api(`/api/tcm-heritage/master-cases?${query}`)" in body


def test_适宜技术表列出操作要点():
    start = CLINICAL.index("async function renderTcm()")
    tcm = CLINICAL[start:CLINICAL.index("\nasync function ", start + 1)]
    begin = tcm.index('table(["名称", "分类", "适应症", "操作要点"], techniques, (t) =>')   # 修前没有这一列
    row = tcm[begin:tcm.index("</tr>`", begin)]
    assert '<td style="white-space:pre-wrap">${esc(t.description) || "—"}</td>' in row
