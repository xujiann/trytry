"""公卫事件的「简要情况」录得进、看得见（P2-1669，第四十九批扫描 AM4-7）。

`EventCreate.description`（最长 1024）接口一直收、事件清单也一直返回；修前「公卫协同」页的立案表单只有名称、级别、病种，
事件列表与「查看处置」都不显示它——整个 `renderPublicHealth` 里 `description` 出现 0 次：立案时写不了地点、波及人数、首发
时间这类概况，经接口写进去的概况处置人员在页面上也看不到（扫描实测：建事件带「某小学 3 个班 21 人腹泻呕吐」201，页面上无处可见）。

修后（接口不改）：立案表单加「简要情况」多行框（`maxlength` 照模型的 1024），页面原样经 `formJson` 送 `description`；
「查看处置」那一段在处置记录上方印出简要情况，一律 `esc()`、`white-space:pre-wrap` 保留换行，没填的写 —。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.routers.publichealth import EventCreate

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
B = "/api/publichealth"
#: 简要情况里夹一段脚本：显示时必须原样转义，不能进 DOM
PAYLOAD = "<script>alert(1)</script>"
DESCRIPTION = f"某小学 3 个班 21 人腹泻呕吐\n首发 10-08 午餐后 {PAYLOAD}"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")

#: `$()` 按选择器给一个记 innerHTML 的假元素；`api()` 按完整路径回给定数据；`postAction` 记下页面要送的路径与请求体；
#: `FormData` 按假表单的 `fields`（DOM 次序的 [name, value]）给出 entries
_HARNESS = """
let els = {};
const posted = [];
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", innerHTML: "", className: "", listeners: {},
    classList: { add() {}, remove() {} }, addEventListener(type, fn) { this.listeners[type] = fn; } }); } };
globalThis.FormData = class { constructor(form) { this.form = form; }
  entries() { return this.form.fields[Symbol.iterator](); }
  get(name) { const hit = this.form.fields.find(([k]) => k === name); return hit ? hit[1] : null; } };
const DATA = JSON.parse(process.argv[1]);
async function api(path) {
  if (!(path in DATA.get)) throw new Error("页面多取了 " + path);
  return DATA.get[path];
}
async function route() {}
function postAction(path, body) { posted.push([path, body]); }
async function spdModal() { return null; }
const PH_EVENT_VIEW = { id: DATA.view };
"""


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def _run_page(get: dict, view: int, fields: list[list[str]]) -> dict:
    """渲染公卫协同页（`view` 是「查看处置」展开的那一起），再按 `fields` 提交一次立案表单，回页面 HTML 与要送的请求。"""
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    page = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    script = (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8")
              + _top_level(core, "function table(") + _top_level(core, "function panel(")
              + _top_level(core, "function setMsg(") + _top_level(core, "function actionableFirst(")
              + _top_level(page, "function formJson(") + _top_level(page, "async function renderPublicHealth(")
              + "(async () => { await renderPublicHealth();\n"
              "  const body = els['#page-body'].innerHTML;\n"
              "  els['#ev-form'].onsubmit({ preventDefault() {},"
              " target: { fields: DATA.fields, querySelectorAll() { return []; } } });\n"
              "  process.stdout.write(JSON.stringify({ body, posted })); })();\n")
    data = {"get": get, "view": view, "fields": fields}
    out = subprocess.run(["node", "-e", script, json.dumps(data, ensure_ascii=False)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _reads(client, admin, view: int = 0) -> dict:
    paths = [f"{B}/events", f"{B}/events?status=active", f"{B}/monitors", "/api/organizations"]
    if view:
        paths.append(f"{B}/events/{view}/actions")
    return {path: client.get(path, headers=admin).json() for path in paths}


def _ev_form(body: str) -> str:
    start = body.index('<form class="inline" id="ev-form">')
    return body[start:body.index("</form>", start)]


def test_简要情况框的上限照模型():
    meta = EventCreate.model_fields["description"].metadata
    limit = next(m.max_length for m in meta if hasattr(m, "max_length"))
    source = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    page = _top_level(source, "async function renderPublicHealth(")
    assert re.search(rf'<textarea name="description" rows="3" maxlength="{limit}"', _ev_form(page))   # 修前没有这个框


@needs_node
def test_立案表单录得进简要情况_页面送的键名接口照收(client, admin):
    out = _run_page(_reads(client, admin), 0, [])
    form = _ev_form(out["body"])
    names = re.findall(r'<(?:input|select|textarea) name="(\w+)"', form)
    assert names == ["title", "level", "disease_name", "description"]              # 修前没有 description
    assert set(names) <= set(EventCreate.model_fields)
    values = {"title": "P21669 甲镇学校聚集性腹泻", "level": "III", "disease_name": "诺如病毒感染",
              "description": DESCRIPTION}
    out = _run_page(_reads(client, admin), 0, [[name, values[name]] for name in names])
    ((path, body),) = out["posted"]
    assert path == f"{B}/events" and body == values                               # formJson 原样送上去
    made = client.post(path, headers=admin, json=body)
    assert made.status_code == 201, made.text
    listed = {e["id"]: e for e in client.get(f"{B}/events", headers=admin).json()}
    assert listed[made.json()["id"]]["description"] == DESCRIPTION


@needs_node
def test_查看处置印出简要情况_转义且保留换行_没填写横线(client, admin):
    full = client.post(f"{B}/events", headers=admin, json={
        "title": "P21669 乙村食源性疾病", "level": "IV", "description": DESCRIPTION}).json()
    bare = client.post(f"{B}/events", headers=admin, json={"title": "P21669 丙镇不明原因发热"}).json()
    html = _run_page(_reads(client, admin, full["id"]), full["id"], [])["body"]
    assert ('<p class="desc">简要情况：<span style="white-space:pre-wrap">'
            "某小学 3 个班 21 人腹泻呕吐\n首发 10-08 午餐后 &lt;script&gt;alert(1)&lt;/script&gt;</span></p>") in html
    assert PAYLOAD not in html                                                     # 简要情况里的标签不进 DOM
    html = _run_page(_reads(client, admin, bare["id"]), bare["id"], [])["body"]
    assert '<p class="desc">简要情况：<span style="white-space:pre-wrap">—</span></p>' in html
    # 不展开哪一起时不印
    assert "简要情况：" not in _run_page(_reads(client, admin), 0, [])["body"]
