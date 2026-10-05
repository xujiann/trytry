"""直播回放地址只收 http(s)、直播表「回放」列只给 http(s) 画链接（P2-1429，第四十二批「远程医学教育与培训考核」扫描 AF1-4
的回放地址一半）。

修前 `LiveRecording.recording_url` 只判非空：`javascript:alert(document.cookie)`、`data:text/html,…`、相对路径、`ftp://…`
挂回放一律 200；直播表（`renderEducation`）把它原样放进 `<a href>`，只做了 `esc()`——`esc()` 转的是引号与尖括号、不管协议，
而 main.py 的 CSP 为免构建的内联脚本放行了 'unsafe-inline'，`javascript:` 链接点了就在本站执行。与收银页 pay_url「只认
http(s)」（P2-1021）的口径不一致。

修后：接口只收 http(s)（不分大小写），其余 422、中文报错；存量的照原样读出。「回放」列只给 http(s) 画链接，存量的非 http(s)
地址照原样转义成文字、不做 href。页面上判 http(s) 收成 core.js 的 `isHttpUrl` 一处（收银页付款链接、课件外链、直播回放
三处共用）。直播的归属与权限（谁能审、谁能挂回放）不在这一条。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import login
from jssrc import strip_comments

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
B = "/api/education"
#: 带 `&` 与 `<` 的回放地址：href 照样转义
HTTPS_URL = "https://vod.example/replay/1.mp4?a=1&b=<x>"
LEGACY = {
    "js": "javascript:alert(document.cookie)",
    "js_upper": "JAVASCRIPT:alert(1)",
    "data": "data:text/html,<script>alert(1)</script>",
    "rel": "/static/replay.mp4",
    "ftp": "ftp://vod.example/replay.mp4",
}


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


@pytest.fixture(scope="module")
def staff(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21429 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    for username, role in (("p21429_doc", "doctor"), ("p21429_op", "operator")):
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pw123456", "role": role, "org_id": org})
        assert resp.status_code in (200, 201), resp.text
    return {"doctor": login(client, "p21429_doc", "pw123456"), "operator": login(client, "p21429_op", "pw123456")}


def _finished(client, admin, staff, title: str) -> int:
    """申请 → 审核通过 → 结束：只有已结束的直播能挂回放。"""
    sid = client.post(f"{B}/live-sessions", headers=staff["doctor"], json={"title": title}).json()["id"]
    assert client.post(f"{B}/live-sessions/{sid}/review?approve=true", headers=admin).status_code == 200
    assert client.post(f"{B}/live-sessions/{sid}/finish", headers=staff["operator"]).status_code == 200
    return sid


def _set_legacy(session_id: int, url: str) -> None:
    """直接改库：修后接口收不进非 http(s) 的回放地址，修前挂上去的照样在库里。"""
    from app.database import SessionLocal
    from app.models import LiveSession

    db = SessionLocal()
    try:
        db.get(LiveSession, session_id).recording_url = url
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------- 接口


@pytest.mark.parametrize("url", list(LEGACY.values()) + ["vod.example/replay.mp4"])
def test_回放地址只收http_s_其余422中文报错(client, admin, staff, url):
    sid = _finished(client, admin, staff, "P21429 坏回放")
    resp = client.post(f"{B}/live-sessions/{sid}/recording", headers=staff["operator"], json={"recording_url": url})
    assert resp.status_code == 422, resp.text   # 修前 200
    (err,) = resp.json()["detail"]
    assert err["loc"] == ["body", "recording_url"] and "回放地址须以 http:// 或 https:// 开头" in err["msg"], err
    row = next(r for r in client.get(f"{B}/live-sessions", headers=admin).json() if r["id"] == sid)
    assert row["recording_url"] == ""   # 没挂上


@pytest.mark.parametrize("url", [HTTPS_URL, "HTTP://VOD.EXAMPLE/REPLAY.MP4"])
def test_http_s回放地址照收(client, admin, staff, url):
    sid = _finished(client, admin, staff, "P21429 好回放")
    resp = client.post(f"{B}/live-sessions/{sid}/recording", headers=staff["operator"], json={"recording_url": url})
    assert resp.status_code == 200 and resp.json() == {"id": sid, "recording_url": url}, resp.text


def test_存量的非http_s回放地址照原样读出(client, admin, staff):
    sid = _finished(client, admin, staff, "P21429 存量回放")
    _set_legacy(sid, LEGACY["ftp"])
    row = next(r for r in client.get(f"{B}/live-sessions", headers=admin).json() if r["id"] == sid)
    assert row["recording_url"] == LEGACY["ftp"]


# ---------------------------------------------------------------- 页面（node 里原样跑 renderEducation）


@pytest.fixture(scope="module")
def live_rows(client, admin, staff):
    """一场挂 https 回放、五场挂存量的非 http(s) 地址、一场没挂；页面画出来的直播表按场次取「回放」列。"""
    ids = {"https": _finished(client, admin, staff, "P21429 页面 https")}
    assert client.post(f"{B}/live-sessions/{ids['https']}/recording", headers=staff["operator"],
                       json={"recording_url": HTTPS_URL}).status_code == 200
    for key, url in LEGACY.items():
        ids[key] = _finished(client, admin, staff, f"P21429 页面 {key}")
        _set_legacy(ids[key], url)
    ids["none"] = _finished(client, admin, staff, "P21429 页面没挂")
    responses = {}
    for path in ("/api/education/courses", "/api/education/my-records", "/api/education/live-sessions"):
        resp = client.get(path, headers=admin)
        assert resp.status_code == 200, (path, resp.text)
        responses[path] = resp.json()
    core = _src("core.js")
    script = (
        "const elements = new Map();\n"
        "function el(sel) {\n"
        "  if (!elements.has(sel)) elements.set(sel, { innerHTML: '', textContent: '', className: '' });\n"
        "  return elements.get(sel);\n"
        "}\n"
        "globalThis.document = { addEventListener() {}, querySelector: el, cookie: '' };\n"
        + _src("shared.js")
        + _function(core, "function table(") + _function(core, "function panel(")
        + _optional_function(core, "function isHttpUrl(")   # 修前没有
        + "function currentRole() { return 'director'; }\n"
        + _function(_src("pages-clinical.js"), "async function renderEducation(")
        + "const RESPONSES = JSON.parse(process.argv[1]);\n"
        "async function api(path) {\n"
        "  if (!(path in RESPONSES)) throw new Error(`没料到的请求：${path}`);\n"
        "  return JSON.parse(JSON.stringify(RESPONSES[path]));\n"
        "}\n"
        "async function drawEduGaps() {}\n"
        "(async () => { await renderEducation(); process.stdout.write(el('#page-body').innerHTML); })()\n"
        "  .catch((err) => { console.error(err); process.exit(1); });\n"
    )
    done = subprocess.run(["node", "-e", script, json.dumps(responses, ensure_ascii=False)],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    html = done.stdout
    lives = html[html.index("直播管理"):html.index("健康宣教文章")]
    cells = {}
    for row in re.findall(r"<tr>(.*?)</tr>", lives, re.S):
        tds = [c.strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if tds:
            cells[int(tds[0])] = tds[6]   # ID / 主题 / 主讲 / 计划时间 / 状态 / 审核意见 / 回放 / 操作
    return {"ids": ids, "replay": cells}


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_回放列只给http_s画链接(live_rows):
    ids, replay = live_rows["ids"], live_rows["replay"]
    assert replay[ids["https"]] == f'<a href="{_esc(HTTPS_URL)}" target="_blank" rel="noopener">回放</a>'
    assert replay[ids["none"]] == "—"


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_存量的非http_s回放地址转义成文字_不做href(live_rows):
    ids, replay = live_rows["ids"], live_rows["replay"]
    for key, url in LEGACY.items():
        assert replay[ids[key]] == _esc(url), (key, replay[ids[key]])   # 修前 <a href="javascript:…">回放</a>


def test_页面判http_s只有core_js一处_收银页付款链接也走它():
    """三处（收银页付款链接 P2-1021、课件外链 P2-1428、直播回放 P2-1429）共用 core.js 的 isHttpUrl；正则只写在它里面。"""
    core = strip_comments(_src("core.js"))
    helper = _function(core, "function isHttpUrl(")
    assert "return /^https?:\\/\\//i.test(url || \"\");" in helper
    defined = [p.name for p in sorted(STATIC.rglob("*.js")) if "function isHttpUrl(" in p.read_text(encoding="utf-8")]
    assert defined == ["core.js"], defined
    for name in ("pages-clinical.js", "pages-public.js", "shared.js"):
        assert "/^https?:\\/\\//i" not in strip_comments(_src(name)), f"{name} 里又自己写了一份 http(s) 判据"
    clinical = strip_comments(_src("pages-clinical.js"))
    assert "if (isHttpUrl(order.pay_url)) {" in clinical   # 收银页只换调用、不改行为
    assert "isHttpUrl(s.recording_url)" in _function(clinical, "async function renderEducation(")
    public = strip_comments(_src("pages-public.js"))
    assert "isHttpUrl(url)" in _function(public, "function playMaterial(")
    assert "isHttpUrl(m.url)" in _function(public, "async function drawEduGaps(")
