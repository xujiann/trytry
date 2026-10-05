"""慢专病宣教资料的链接只收 http(s)、居民端「我的宣教」只给 http(s) 画链接（P2-1465，P2-1428 / P2-1429 的同类跟进）。

修前 `spd/routers/config/scales.py` 的 `EduIn.media_url` / `EduPatch.media_url` 只限长度：管理层、医师、公卫建或改宣教素材时
填 `javascript:alert(document.cookie)`、`data:text/html,…`、相对路径、`ftp://…` 一律照收；推送给居民后，居民端 `m/m.js` 的
`renderSpdEdu` 把它原样放进 `<a href>`，只做了 `esc()`——`esc()` 转的是引号与尖括号、不管协议，而 main.py 的 CSP 为免构建的
内联脚本放行了 'unsafe-inline'，居民点「资料」就在居民端执行脚本。P2-1429 修直播回放时登记了「居民端宣教资料链接同类问题另行
处理」，这一条就是它。

修后：建档、改档的资料地址只收空串或 http(s)（与课件外链、直播回放共用 `texttypes.check_http_url`），其余 422、中文报错；
存量的照原样读出。居民端只给 http(s) 画链接，其余照原样转义成文字。页面上的判据 `isHttpUrl` 从 core.js 挪到三端都加载的
shared.js（core.js 原先的注释写明「居民端哪天也要判，再挪过去」），一处判据。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from jssrc import strip_comments

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
B = "/api/spd/edu-materials"
HTTPS_URL = "https://cdn.example/edu/salt.mp3?a=1&b=<x>"
BAD_URLS = [
    "javascript:alert(document.cookie)",
    "JaVaScRiPt:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "/static/salt.mp4",                  # 相对路径：点开是本站别的页面
    "cdn.example/salt.mp4",
    "ftp://files.example/salt.mp4",
    " https://cdn.example/salt.mp4",     # 前面带空格：页面按 /^https?:\/\//i 不认它是链接，后端同一判据，不替人 strip
]


def _esc(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def _create(client, admin, code: str, **extra):
    return client.post(B, headers=admin, json={"code": code, "title": f"{code} 宣教", "content": "少盐", **extra})


# ---------------------------------------------------------------- 接口：建档、改档只收空串或 http(s)


@pytest.mark.parametrize("url", BAD_URLS)
def test_建档的资料地址只收空串或http_s_其余422中文报错(client, admin, url):
    resp = _create(client, admin, f"p21465_bad_{BAD_URLS.index(url)}", media_type="audio", media_url=url)
    assert resp.status_code == 422, resp.text   # 修前 201
    (err,) = resp.json()["detail"]
    assert err["loc"] == ["body", "media_url"] and "资料地址须以 http:// 或 https:// 开头" in err["msg"], err


@pytest.mark.parametrize("url", [HTTPS_URL, "HTTP://CDN.EXAMPLE/SALT.MP4", ""])
def test_http_s与空串照收(client, admin, url):
    resp = _create(client, admin, f"p21465_ok_{len(url)}", media_type="audio", media_url=url)
    assert resp.status_code == 201, resp.text
    assert resp.json()["media_url"] == url


def test_改档同建档_传了坏地址422_不传不动(client, admin):
    material = _create(client, admin, "p21465_patch", media_type="video", media_url=HTTPS_URL).json()
    bad = client.patch(f"{B}/{material['id']}", headers=admin, json={"media_url": "javascript:alert(1)"})
    assert bad.status_code == 422, bad.text   # 修前 200，改档能把好地址换成 javascript:
    assert "资料地址须以 http:// 或 https:// 开头" in bad.json()["detail"][0]["msg"]
    title_only = client.patch(f"{B}/{material['id']}", headers=admin, json={"title": "p21465 只改标题"})
    assert title_only.status_code == 200 and title_only.json()["media_url"] == HTTPS_URL, title_only.text
    cleared = client.patch(f"{B}/{material['id']}", headers=admin, json={"media_url": ""})
    assert cleared.status_code == 200 and cleared.json()["media_url"] == "", cleared.text


def test_存量的非http_s地址照原样读出_出参不校验(client, admin):
    from app.database import SessionLocal
    from app.models import SpdEduMaterial

    with SessionLocal() as db:
        row = SpdEduMaterial(code="p21465_legacy", title="p21465 存量", media_type="video",
                             media_url="javascript:alert(document.cookie)")
        db.add(row)
        db.commit()
        mid = row.id
    resp = client.get(f"{B}?keyword=p21465 存量", headers=admin)
    assert resp.status_code == 200, resp.text
    assert [m["media_url"] for m in resp.json() if m["id"] == mid] == ["javascript:alert(document.cookie)"]


# ---------------------------------------------------------------- 居民端：「我的宣教」只给 http(s) 画链接


def _function(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 2]


#: 「我的宣教」的卡片：一张 https、四张存量的非 http(s)、一张没有资料（各带自己的 id，按「标记已读」按钮认卡片）
ROWS = [
    {"id": 1, "media_url": HTTPS_URL, "media_type_name": "音频"},
    {"id": 2, "media_url": "javascript:alert(document.cookie)", "media_type_name": "视频"},
    {"id": 3, "media_url": "JAVASCRIPT:alert(1)", "media_type_name": "视频"},
    {"id": 4, "media_url": "data:text/html,<script>alert(1)</script>", "media_type_name": "视频"},
    {"id": 5, "media_url": "/static/salt.mp4", "media_type_name": "视频"},
    {"id": 6, "media_url": "", "media_type_name": "图文"},
]


@pytest.fixture(scope="module")
def cards():
    if shutil.which("node") is None:
        pytest.skip("没有 node 执行居民端渲染")
    m_js = (STATIC / "m" / "m.js").read_text(encoding="utf-8")
    rows = [{"title": f"宣教{r['id']}", "content": "", "created_at": "2026-10-05T08:00:00", "status": "unread", **r}
            for r in ROWS]
    script = (
        "globalThis.document = { addEventListener() {}, querySelector() { return null; }, cookie: '' };\n"
        + (STATIC / "shared.js").read_text(encoding="utf-8")
        + "let viewingPatientId = null;\n"
        + _function(m_js, "function kv(") + _function(m_js, "function spdQuery(")
        + _function(m_js, "async function renderSpdEdu(")
        + "const ROWS = JSON.parse(process.argv[1]);\n"
        "async function authApi() { return JSON.parse(JSON.stringify(ROWS)); }\n"
        "const box = { innerHTML: '', querySelectorAll() { return []; } };\n"
        "(async () => { await renderSpdEdu(box); process.stdout.write(box.innerHTML); })()\n"
        "  .catch((err) => { console.error(err); process.exit(1); });\n"
    )
    done = subprocess.run(["node", "-e", script, json.dumps(rows, ensure_ascii=False)],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    out = {}
    for card in done.stdout.split('<div class="m-card">')[1:]:
        cid = int(card.split('data-spd-edu-read="', 1)[1].split('"', 1)[0])
        out[cid] = card
    assert sorted(out) == [r["id"] for r in ROWS], done.stdout
    return out


def test_只有http_s的资料画成链接(cards):
    assert f'<a href="{_esc(HTTPS_URL)}" target="_blank" rel="noopener">音频</a>' in cards[1]


def test_存量的非http_s资料转义成文字_不做href(cards):
    for row in ROWS[1:5]:
        card = cards[row["id"]]
        assert "<a " not in card and "href=" not in card, card   # 修前 <a href="javascript:…">视频</a>
        assert f'<span class="k">资料</span><span>{_esc(row["media_url"])}</span>' in card, card


def test_没有资料的不出资料这一行(cards):
    assert "资料" not in cards[6]


def test_居民端经shared_js的isHttpUrl判_不再自己写正则():
    m_js = strip_comments((STATIC / "m" / "m.js").read_text(encoding="utf-8"))
    assert "isHttpUrl(p.media_url)" in _function(m_js, "async function renderSpdEdu(")
    assert "/^https?:\\/\\//i" not in m_js, "居民端又自己写了一份 http(s) 判据"
    index = (STATIC / "m" / "index.html").read_text(encoding="utf-8")
    assert index.index("/static/shared.js") < index.index("/static/m/m.js")   # 判据在 shared.js，得先加载
