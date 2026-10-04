"""卫生监测清单不显示机构和日期，标红的「超标」看不出是哪家、哪天（P2-1436，第四十二批扫描 AF2-10）。

「公卫协同」页的「其他卫生业务监测」清单只有领域、指标、值/阈值、状态四列，接口本来就返回 `org_id` 与 `record_date`：各机构的
监测记录混在一张表里（只取最新 200 条），超标的那一行没法下去复核；接口收 `exceeded` 参数，页面也没有「只看超标」。同页之外的
症候群日报表、病原日报表都显示机构和日期。

修法（只改页面）：清单加「机构」「日期」两列——机构名按 `/api/organizations` 映射，映射不到回显编号，一律 `esc()`；没填日期
印「—」。加「只看超标」筛选，照本页诊间提醒查询的写法：先清空、查不到把原因写出来。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
B = "/api/publichealth"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")


@pytest.fixture(scope="module")
def world(client, admin):
    def org(name):
        return client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township", "level": "township"}).json()["id"]

    a, b = org("P21436 甲镇<卫生院>"), org("P21436 乙村卫生室")
    for body in ({"domain": "environment", "org_id": a, "indicator": "余氯 mg/L", "value": 0.8, "threshold": 0.3,
                  "record_date": "2026-09-30"},
                 {"domain": "school", "org_id": b, "indicator": "教室照度 lx", "value": 150, "threshold": 300}):
        made = client.post(f"{B}/monitors", headers=admin, json=body)
        assert made.status_code == 201, made.text
    get = {path: client.get(path, headers=admin).json() for path in (
        f"{B}/events", f"{B}/events?status=active", f"{B}/monitors", f"{B}/monitors?exceeded=true",
        "/api/organizations")}
    return {"a": a, "b": b, "get": get}


def _top_level(source: str, head: str, end: str = "\n}\n") -> str:
    start = source.index(head)
    return source[start:source.index(end, start) + len(end)]


#: `$()` 按选择器给一个记 innerHTML 的假元素；`api()` 按完整路径回给定数据（末尾空的 `?` 按不带参数认；`DATA.fail` 里的
#: 路径抛错），并记下请求过的地址；`FormData` 读假表单的 `values`（勾没勾「只看超标」）
_HARNESS = """
let els = {};
const calls = [];
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", innerHTML: "", className: "", listeners: {},
    classList: { add() {}, remove() {} }, addEventListener(type, fn) { this.listeners[type] = fn; } }); } };
globalThis.FormData = class { constructor(form) { this.values = (form && form.values) || {}; }
  get(name) { return this.values[name] ?? null; } };
const DATA = JSON.parse(process.argv[1]);
async function api(path, opts = {}) {
  calls.push(path);
  if ((DATA.fail || []).includes(path)) throw new Error("无权查看：<监测>");
  return DATA.get[path.replace(/\\?$/, "")];
}
async function route() {}
function formJson() { return {}; }
function postAction() {}
async function spdModal() { return null; }
const PH_EVENT_VIEW = { id: 0 };
"""


def _run_page(get: dict, filters: list, fail: list | None = None) -> dict:
    """渲染公卫协同页，再依次按 `filters`（每项是「只看超标」勾没勾）提交筛选，记下每次筛选前后清单区的内容。"""
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    page = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    script = (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8")
              + _top_level(core, "function table(") + _top_level(core, "function panel(")
              + _top_level(core, "function setMsg(") + _top_level(core, "function actionableFirst(")
              + _top_level(page, "async function renderPublicHealth(")
              + "(async () => { await renderPublicHealth();\n"
              "  const body = els['#page-body'].innerHTML, lists = [];\n"
              "  for (const exceeded of DATA.filters) {\n"
              "    els['#mon-list'] = { innerHTML: 'STALE' };\n"
              "    const pending = els['#mon-filter'].onsubmit({ preventDefault() {},"
              " target: { values: exceeded ? { exceeded: '1' } : {} } });\n"
              "    const cleared = els['#mon-list'].innerHTML;\n"
              "    await pending;\n"
              "    lists.push([cleared, els['#mon-list'].innerHTML]);\n"
              "  }\n"
              "  process.stdout.write(JSON.stringify({ body, lists, calls })); })();\n")
    data = {"get": get, "filters": filters, "fail": fail or []}
    out = subprocess.run(["node", "-e", script, json.dumps(data, ensure_ascii=False)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _rows(html: str) -> list[str]:
    """清单表的各行（去掉表头）。"""
    return html[html.index("<tbody>") + len("<tbody>"):html.index("</tbody>")].split("</tr>")[:-1]


@needs_node
def test_清单加机构与日期两列_机构名映射_一律转义_没填日期印横线(world):
    out = _run_page(world["get"], [])
    listing = out["body"][out["body"].index('<div id="mon-list">'):]
    # 修前表头是 领域 / 指标 / 值/阈值 / 状态，超标的看不出是哪家、哪天
    assert "<th>机构</th><th>领域</th><th>指标</th><th>值/阈值</th><th>日期</th><th>状态</th>" in listing
    rows = _rows(listing)
    assert rows[0] == ('<tr><td>P21436 乙村卫生室</td><td>学校</td><td>教室照度 lx</td><td>150 / 300</td><td>—</td>'
                       '<td><span class="tag green">正常</span></td>')
    assert rows[1] == ('<tr><td>P21436 甲镇&lt;卫生院&gt;</td><td>环境</td><td>余氯 mg/L</td><td>0.8 / 0.3</td>'
                       '<td>2026-09-30</td><td><span class="tag red">超标</span></td>')


@needs_node
def test_机构清单里映射不到的回显编号(world):
    get = {**world["get"], "/api/organizations": [o for o in world["get"]["/api/organizations"] if o["id"] != world["b"]]}
    out = _run_page(get, [])
    rows = _rows(out["body"][out["body"].index('<div id="mon-list">'):])
    assert rows[0].startswith(f"<tr><td>{world['b']}</td><td>学校</td>")


@needs_node
def test_只看超标_带exceeded参数_先清空再画(world):
    out = _run_page(world["get"], [True, False])
    assert out["calls"][-2:] == [f"{B}/monitors?exceeded=true", f"{B}/monitors?"]   # 修前页面不带这个参数
    (cleared, exceeded_only), (_, everything) = out["lists"]
    assert cleared == ""   # 发请求之前先清空，上一次的结果不留着
    assert [r[r.index("<td>") + 4:r.index("</td>")] for r in _rows(exceeded_only)] == ["P21436 甲镇&lt;卫生院&gt;"]
    assert len(_rows(everything)) == 2


@needs_node
def test_只看超标_查不到写原因(world):
    out = _run_page(world["get"], [True], fail=[f"{B}/monitors?exceeded=true"])
    assert out["lists"] == [["", '<p class="msg err">无权查看：&lt;监测&gt;</p>']]
