"""课件外链只收 http(s)、清单画出外链、「点播」= 打开并计一次（P2-1428，第四十二批「远程医学教育与培训考核」扫描 AF1-2）。

修前三处对不上：
- 新建课件的表单收「外链地址」，接口 `MaterialCreate.url` 什么都收——`javascript:alert(1)`、`data:text/html,…`、相对路径、
  `ftp://…` 一律 201；
- 课件清单（`drawEduGaps` 里的 `drawMaterials`）根本不读 `m.url`。视频、PPT 只能填外链（附件白名单只有图片与 PDF，传 mp4 /
  pptx 回 415），可外链填进去页面上哪儿都看不到、点不开；
- 「点播」只发 POST `/play` 再整页 `route()`——什么也不打开，只给计数加一，刚查出来的课件清单也被重画冲掉。

修后：接口的外链只收空串或 http(s)（不分大小写，与收银页 pay_url 的 P2-1021 同一口径），其余 422、中文报错；存量的照原样
读出。清单加「外链」列：只给 http(s) 画 `target="_blank" rel="noopener"` 的链接，其余非空值转义成文字，空的写「—」。
「点播」= 打开并计一次：有 http(s) 外链的先同步 `window.open` 再计数（await 之后再开会被弹窗拦截），没有外链但有附件的
计一次并展开附件清单，两样都没有的不摆「点播」；计完只改这一行的点播数，不整页重画。
"""
import io
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from jssrc import strip_comments

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
B = "/api/education"
#: 带 `&` 与 `<` 的外链：href 与文字都得转义
HTTPS_URL = "https://video.example/foot.mp4?a=1&b=<x>"


def _src(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _function(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 2]


def _optional_function(source: str, head: str) -> str:
    """修前没有的函数取成空串：变异检查（撤掉修复）时让用例红在行为上，而不是红在取不到源码上。"""
    return _function(source, head) if head in source else ""


def _esc(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def _legacy(course_id: int, title: str, url: str) -> int:
    """直接落库一条存量课件：修后接口收不进非 http(s) 的外链，修前存进去的照样在库里。"""
    from app.database import SessionLocal
    from app.models import CourseMaterial, User

    db = SessionLocal()
    try:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        row = CourseMaterial(course_id=course_id, title=title, material_type="link", url=url, created_by=admin_id)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _course(client, admin, title: str) -> int:
    resp = client.post(f"{B}/courses", headers=admin, json={"title": title})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _material(client, admin, course_id: int, title: str, url=None) -> dict:
    body = {"title": title, "material_type": "video"}
    if url is not None:
        body["url"] = url
    resp = client.post(f"{B}/courses/{course_id}/materials", headers=admin, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _attach(client, admin, material_id: int, filename: str) -> None:
    resp = client.post("/api/attachments", headers=admin,
                       files={"file": (filename, io.BytesIO(b"%PDF-1.4 P21428"), "application/pdf")},
                       data={"owner_type": "course_material", "owner_id": str(material_id)})
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------- 接口：外链只收空串或 http(s)


@pytest.fixture(scope="module")
def api_course(client, admin):
    return _course(client, admin, "P21428 接口课程")


@pytest.mark.parametrize("url", [
    "javascript:alert(document.cookie)",
    "JaVaScRiPt:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "/static/foot.mp4",                      # 相对路径：点开是本站别的页面
    "video.example/foot.mp4",
    "ftp://files.example/foot.mp4",
    " https://video.example/foot.mp4",       # 前面带空格：页面按 /^https?:\/\//i 不认它是链接，接口同一判据，不替人 strip
])
def test_外链只收空串或http_s_其余422中文报错(client, admin, api_course, url):
    resp = client.post(f"{B}/courses/{api_course}/materials", headers=admin, json={"title": "P21428 坏外链", "url": url})
    assert resp.status_code == 422, resp.text   # 修前 201
    (err,) = resp.json()["detail"]
    assert err["loc"] == ["body", "url"] and "外链地址须以 http:// 或 https:// 开头" in err["msg"], err


@pytest.mark.parametrize("url", [HTTPS_URL, "HTTP://VIDEO.EXAMPLE/FOOT.MP4", ""])
def test_http_s_与空串照收(client, admin, api_course, url):
    assert _material(client, admin, api_course, "P21428 好外链", url)["url"] == url


def test_不填外链照收空串(client, admin, api_course):
    assert _material(client, admin, api_course, "P21428 不填外链")["url"] == ""


def test_存量的非http_s外链照原样读出_出参不校验(client, admin, api_course):
    mid = _legacy(api_course, "P21428 存量外链", "ftp://legacy.example/foot.mp4")
    rows = client.get(f"{B}/courses/{api_course}/materials", headers=admin)
    assert rows.status_code == 200, rows.text
    assert next(r for r in rows.json() if r["id"] == mid)["url"] == "ftp://legacy.example/foot.mp4"
    played = client.post(f"{B}/materials/{mid}/play", headers=admin)
    assert played.status_code == 200 and played.json()["url"] == "ftp://legacy.example/foot.mp4", played.text


# ---------------------------------------------------------------- 页面：清单与「点播」（node 里原样跑 drawEduGaps）


@pytest.fixture(scope="module")
def world(client, admin):
    """一门课八条课件：两条 http(s) 外链、两条只有附件（其一的存量外链是 javascript:）、四条既无可点外链也无附件。"""
    course = _course(client, admin, "P21428 页面课程")
    ids = {
        "https": _material(client, admin, course, "P21428 外链视频", HTTPS_URL)["id"],
        "upper": _material(client, admin, course, "P21428 大写外链", "HTTPS://CDN.EXAMPLE/A.MP4")["id"],
        "att": _material(client, admin, course, "P21428 只有附件")["id"],
        "none": _material(client, admin, course, "P21428 什么都没有")["id"],
        "js": _legacy(course, "P21428 存量 javascript", "javascript:alert(1)"),
        "js_att": _legacy(course, "P21428 存量 javascript 带附件", "JAVASCRIPT:alert(document.cookie)"),
        "data": _legacy(course, "P21428 存量 data", "data:text/html,<script>alert(1)</script>"),
        "rel": _legacy(course, "P21428 存量相对路径", "/static/foot.mp4"),
    }
    _attach(client, admin, ids["att"], "p21428_att.pdf")
    _attach(client, admin, ids["js_att"], "p21428_js_att.pdf")
    return {"course": course, "ids": ids}


def _responses(client, admin, world) -> dict:
    """页面会发的请求 → 接口原样的响应。清单先取（点播前的计数），再逐条点播一次取回执。"""
    draw = _function(_src("pages-public.js"), "async function drawEduGaps(")
    responses = {}
    for path in re.findall(r'\bapi\("([^"]+)"\)', draw):
        resp = client.get(path, headers=admin)
        assert resp.status_code == 200, (path, resp.text)
        responses[f"GET {path}"] = resp.json()
    listed = client.get(f"{B}/courses/{world['course']}/materials", headers=admin)
    assert listed.status_code == 200, listed.text
    responses[f"GET {B}/courses/{world['course']}/materials"] = listed.json()
    for mid in world["ids"].values():
        played = client.post(f"{B}/materials/{mid}/play", headers=admin)
        assert played.status_code == 200, played.text
        responses[f"POST {B}/materials/{mid}/play"] = played.json()
        path = f"/api/attachments?owner_type=course_material&owner_id={mid}"
        responses[f"GET {path}"] = client.get(path, headers=admin).json()
    return responses


def _run(responses: dict, course_id: int) -> dict:
    """把 `drawEduGaps` 原样拿到 node 里跑：查课件清单，再把清单上每个「点播」各点一次，记下发了什么、开了什么窗。"""
    core, clinical, public = _src("core.js"), _src("pages-clinical.js"), _src("pages-public.js")
    script = (
        "const calls = [];\n"
        "const elements = new Map();\n"
        "function el(sel) {\n"
        "  if (!elements.has(sel)) elements.set(sel, { innerHTML: '', textContent: '', className: '' });\n"
        "  return elements.get(sel);\n"
        "}\n"
        "globalThis.document = { addEventListener() {}, querySelector: el, cookie: '' };\n"
        "globalThis.window = { open: (...args) => { calls.push(['open', ...args]); return null; } };\n"
        "globalThis.FormData = class { constructor(form) { this.form = form; } get(name) { return this.form[name]; } };\n"
        + _src("shared.js")
        + _function(core, "function table(") + _function(core, "function panel(") + _function(core, "function setMsg(")
        + _function(clinical, "async function drawAttachments(")
        + re.search(r"^const MATERIAL_TYPES = .*;$", public, re.M).group(0) + "\n"
        + _optional_function(public, "function playMaterial(")
        + _function(public, "async function drawEduGaps(")
        + "const RESPONSES = JSON.parse(process.argv[1]);\n"
        "async function api(path, options = {}) {\n"
        "  const key = `${(options.method || 'GET').toUpperCase()} ${path}`;\n"
        "  calls.push(['api', key]);\n"
        "  if (!(key in RESPONSES)) throw new Error(`没料到的请求：${key}`);\n"
        "  return JSON.parse(JSON.stringify(RESPONSES[key]));\n"
        "}\n"
        "function route() { calls.push(['route']); }\n"
        "const holder = { querySelector: el, onclick: null };\n"
        "function appendSection() { return holder; }\n"
        "const ENTITIES = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '\"', '&#39;': \"'\" };\n"
        "const unescape = (s) => s.replace(/&(?:amp|lt|gt|quot|#39);/g, (m) => ENTITIES[m]);\n"
        "(async () => {\n"
        "  await drawEduGaps();\n"
        "  await el('#cm-query').onsubmit({ preventDefault() {}, target: { course_id: process.argv[2] } });\n"
        "  const listed = el('#cm-list').innerHTML;\n"
        "  const clicks = {};\n"
        "  for (const [, attrs] of listed.matchAll(/<button([^>]*\\bdata-play=\"[^\"]*\"[^>]*)>/g)) {\n"
        "    const dataset = {};\n"
        "    for (const [, name, value] of attrs.matchAll(/\\bdata-(\\w+)=\"([^\"]*)\"/g)) dataset[name] = unescape(value);\n"
        "    calls.length = 0;\n"
        "    el('#cm-att-list').innerHTML = '';\n"
        "    el('#tplan-msg').textContent = '';\n"
        "    const pending = holder.onclick({ target: { dataset } });\n"
        "    const openedInGesture = calls.some((c) => c[0] === 'open');   // 点击处理同步跑完的那一段里就开了窗\n"
        "    await pending;\n"
        "    clicks[dataset.play] = { calls: [...calls], openedInGesture,\n"
        "      plays: String(el(`[data-plays=\"${dataset.play}\"]`).textContent),\n"
        "      attList: el('#cm-att-list').innerHTML, error: el('#tplan-msg').textContent };\n"
        "  }\n"
        "  process.stdout.write(JSON.stringify({ listed, clicks }));\n"
        "})().catch((err) => { console.error(err); process.exit(1); });\n"
    )
    done = subprocess.run(["node", "-e", script, json.dumps(responses, ensure_ascii=False), str(course_id)],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.fixture(scope="module")
def rendered(client, admin, world):
    responses = _responses(client, admin, world)
    before = {m["id"]: m["play_count"] for m in responses[f"GET {B}/courses/{world['course']}/materials"]}
    return {"out": _run(responses, world["course"]), "before": before}


def _rows(html: str) -> dict:
    """`{课件ID: [各列…]}`（表头是 th，不进来）。"""
    rows = {}
    for row in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        cells = [c.strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if cells:
            rows[int(cells[0])] = cells
    return rows


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_清单只给http_s外链画链接_其余转义成文字_空的写横杠(world, rendered):
    html = rendered["out"]["listed"]
    assert "<th>外链</th>" in html   # 修前清单没有这一列
    ids, rows = world["ids"], _rows(html)
    link = '<a href="{0}" target="_blank" rel="noopener">{0}</a>'
    assert rows[ids["https"]][3] == link.format(_esc(HTTPS_URL))
    assert rows[ids["upper"]][3] == link.format("HTTPS://CDN.EXAMPLE/A.MP4")   # 不分大小写
    for key, url in (("js", "javascript:alert(1)"), ("js_att", "JAVASCRIPT:alert(document.cookie)"),
                     ("data", "data:text/html,<script>alert(1)</script>"), ("rel", "/static/foot.mp4")):
        assert rows[ids[key]][3] == _esc(url), (key, rows[ids[key]])   # 照原样转义成文字，不成链接
    assert rows[ids["att"]][3] == rows[ids["none"]][3] == "—"
    assert html.count("<a ") == 2 and "javascript:" not in html.split("<a ", 1)[1].split("</a>", 1)[0]


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_两样都没有的不摆点播(world, rendered):
    ids = world["ids"]
    assert set(rendered["out"]["clicks"]) == {str(ids[k]) for k in ("https", "upper", "att", "js_att")}
    rows = _rows(rendered["out"]["listed"])
    for key in ("none", "js", "data", "rel"):
        assert rows[ids[key]][-1] == "—", (key, rows[ids[key]])   # 修前每行都有「点播」


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_点播_有外链的先同步开窗再计数_只改这一行(world, rendered):
    ids, clicks = world["ids"], rendered["out"]["clicks"]
    for key, url in (("https", HTTPS_URL), ("upper", "HTTPS://CDN.EXAMPLE/A.MP4")):
        mid = ids[key]
        click = clicks[str(mid)]
        assert click["calls"] == [["open", url, "_blank", "noopener"], ["api", f"POST {B}/materials/{mid}/play"]], click
        assert click["openedInGesture"], "开窗要在点击处理同步的那一段里，await 之后再开会被弹窗拦截"
        assert click["plays"] == str(rendered["before"][mid] + 1)   # 只改这一行的点播数
        assert ["route"] not in click["calls"] and click["error"] == ""   # 修前整页 route()，查出来的清单被冲掉


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_点播_没有外链有附件的计一次并展开附件清单(world, rendered):
    ids, clicks = world["ids"], rendered["out"]["clicks"]
    for key, filename in (("att", "p21428_att.pdf"), ("js_att", "p21428_js_att.pdf")):
        mid = ids[key]
        click = clicks[str(mid)]
        assert click["calls"] == [
            ["api", f"POST {B}/materials/{mid}/play"],
            ["api", f"GET /api/attachments?owner_type=course_material&owner_id={mid}"],
        ], click   # 存量 javascript: 外链不开窗
        assert filename in click["attList"] and click["plays"] == str(rendered["before"][mid] + 1), click
        assert click["error"] == ""


def test_手册写的附件范围与附件接口一致_视频PPT填外链():
    """手册原先写「可按课件 ID 上传附件（课件 / 视频 / 文档）」——附件白名单只有图片与 PDF、每个不超过 10MB，传 mp4 / pptx
    回 415。手册里写下的类型与大小由附件接口的常量盯着：接口改了，这里提醒把手册一起改。"""
    from app.routers import attachments

    manual = (Path(__file__).resolve().parents[2] / "docs" / "用户手册.md").read_text(encoding="utf-8")
    item = manual[manual.index("5. **健康宣教**"):]
    item = item[:item.index("\n6. **")]
    assert "（课件 / 视频 / 文档）" not in item
    assert "附件只收图片与 PDF（每个不超过 10MB）" in item and "视频、PPT 等在新增课件时填外链地址" in item
    assert attachments.MAX_SIZE_BYTES == 10 * 1024 * 1024
    assert {t for t in attachments.ALLOWED_CONTENT_TYPES if not t.startswith("image/")} == {"application/pdf"}


def test_点播先开窗再计数_计完不整页重画():
    """开窗写在单列的 playMaterial 里、在发计数之前（任何 await 之前，见 test_window_open_before_await.py）；
    drawEduGaps 的点播分支经它点播，不再自己发计数、不再 route()。"""
    public = strip_comments(_src("pages-public.js"))
    play = _function(public, "function playMaterial(")
    assert play.index('window.open(url, "_blank", "noopener")') < play.index("api(`/api/education/materials/")
    draw = _function(public, "async function drawEduGaps(")
    branch = draw[draw.index("if (play) {"):]
    branch = branch[:branch.index("\n      }\n")]
    assert "playMaterial(play, url)" in branch and "route()" not in branch, branch
    assert "/play`" not in branch
