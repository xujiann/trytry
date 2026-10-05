"""实训计划的适宜技术从技术库里选、计划表写技术名称（P2-1430，第四十二批「远程医学教育与培训考核」扫描 AF1-6）。

修前发布计划要手填「适宜技术ID」（数字框），而适宜技术库页面（中医药服务页）只列名称、分类、适应症、操作要点，不显示编号——
页面上查不到编号；填一个别的、但确实存在的编号照样 201，计划就挂到另一项技术上。建好以后计划表也没有技术这一列，出参只回
`technique_id` 编号，考核对应哪项适宜技术界面上看不到。

修后：表单的数字框换成下拉（取 `/api/tcm/techniques`，选项写技术名，首项「不挂适宜技术」= 空；技术库只要登录就能读，
万一没取到，下拉只剩首项并说一句，不把整页掀掉）；计划表加「适宜技术」列（名称，没挂的写「—」，一律 esc()）；
实训计划出参（`TrainingPlanOut`，新建回执与清单同形）末尾只增 `technique_name`（没挂或查不到为 null），原有键与次序不动。
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
#: 修前出参的键与次序：一个不动，technique_name 只加在末尾
OLD_KEYS = ["id", "title", "technique_id", "org_id", "plan_date", "capacity", "trainer", "status", "status_name",
            "enrolled", "remaining"]
#: 技术名里夹一段标签：下拉与计划表都得转义
NAME_PAYLOAD = "P21430 刮痧<b>x</b>"


def _src(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _function(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 2]


def _esc(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21430 中医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    resp = client.post("/api/users", headers=admin, json={
        "username": "p21430_dir", "password": "pw123456", "role": "director", "org_id": org})
    assert resp.status_code in (200, 201), resp.text
    director = login(client, "p21430_dir", "pw123456")
    techniques = {}
    for name in ("P21430 艾灸足三里", NAME_PAYLOAD):
        resp = client.post("/api/tcm/techniques", headers=admin, json={"name": name, "category": "外治"})
        assert resp.status_code == 201, resp.text
        techniques[name] = resp.json()["id"]
    plans = {}
    for key, technique in (("with", techniques[NAME_PAYLOAD]), ("without", None)):
        body = {"title": f"P21430 实训 {key}", "org_id": org, "plan_date": "2026-10-20"}
        if technique is not None:
            body["technique_id"] = technique
        resp = client.post(f"{B}/training-plans", headers=director, json=body)
        assert resp.status_code == 201, resp.text
        plans[key] = resp.json()
    return {"org": org, "director": director, "techniques": techniques, "plans": plans}


# ---------------------------------------------------------------- 接口：出参末尾带技术名称


def test_新建回执末尾带适宜技术名称_原有键与次序不动(world):
    receipt = world["plans"]["with"]
    assert list(receipt) == OLD_KEYS + ["technique_name"]   # 修前没有 technique_name
    assert receipt["technique_id"] == world["techniques"][NAME_PAYLOAD]
    assert receipt["technique_name"] == NAME_PAYLOAD
    assert world["plans"]["without"]["technique_id"] is None and world["plans"]["without"]["technique_name"] is None


def test_计划清单每行带适宜技术名称_没挂的为null(client, world):
    rows = {r["id"]: r for r in client.get(f"{B}/training-plans", headers=world["director"]).json()}
    with_row, without_row = rows[world["plans"]["with"]["id"]], rows[world["plans"]["without"]["id"]]
    assert list(with_row) == OLD_KEYS + ["technique_name"]
    assert (with_row["technique_name"], without_row["technique_name"]) == (NAME_PAYLOAD, None)
    assert with_row == world["plans"]["with"]   # 新建回执与清单行同形


def test_技术库只要登录就能读_下拉各角色都取得到(client, admin, world):
    """下拉取 /api/tcm/techniques：路由只挂了登录校验，内置的五种业务角色都读得到（没取到的兜底见页面用例）。"""
    for role in ("director", "doctor", "pharmacist", "public_health", "operator"):
        username = f"p21430_read_{role}"
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pw123456", "role": role, "org_id": world["org"]})
        assert resp.status_code in (200, 201), resp.text
        got = client.get("/api/tcm/techniques", headers=login(client, username, "pw123456"))
        assert got.status_code == 200 and {t["name"] for t in got.json()} >= set(world["techniques"]), (role, got.text)


# ---------------------------------------------------------------- 页面（node 里原样跑 drawEduGaps）


def _render(responses: dict, broken: str = "") -> str:
    """跑 `drawEduGaps`，收下它交给 appendSection 的整块 HTML；`broken` 那个接口按失败回。"""
    core = _src("core.js")
    public = _src("pages-public.js")
    script = (
        "globalThis.document = { addEventListener() {}, querySelector() { return {}; }, cookie: '' };\n"
        + _src("shared.js")
        + _function(core, "function table(") + _function(core, "function panel(")
        + re.search(r"^const MATERIAL_TYPES = .*;$", public, re.M).group(0) + "\n"
        + _function(public, "async function drawEduGaps(")
        + "const RESPONSES = JSON.parse(process.argv[1]);\n"
        "const BROKEN = process.argv[2];\n"
        "async function api(path) {\n"
        "  if (path === BROKEN) throw new Error('取数失败');\n"
        "  if (!(path in RESPONSES)) throw new Error(`没料到的请求：${path}`);\n"
        "  return JSON.parse(JSON.stringify(RESPONSES[path]));\n"
        "}\n"
        "let drawn = '';\n"
        "function appendSection(html) { drawn += html; return { querySelector() { return {}; } }; }\n"
        "(async () => { await drawEduGaps(); process.stdout.write(drawn); })()\n"
        "  .catch((err) => { console.error(err); process.exit(1); });\n"
    )
    done = subprocess.run(["node", "-e", script, json.dumps(responses, ensure_ascii=False), broken],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _responses(client, admin) -> dict:
    responses = {}
    for path in re.findall(r'\bapi\("([^"]+)"\)', _function(_src("pages-public.js"), "async function drawEduGaps(")):
        resp = client.get(path, headers=admin)
        assert resp.status_code == 200, (path, resp.text)
        responses[path] = resp.json()
    return responses


def _plan_form(html: str) -> str:
    start = html.index('<form class="inline" id="tp-plan-form">')
    return html[start:html.index("</form>", start)]


def _plan_cells(html: str) -> dict:
    """计划表 `{计划ID: 适宜技术列}`：ID / 主题 / 适宜技术 / 日期 / …"""
    plans = html[html.index('id="tplan-msg"'):]
    return {int(pid): tech for pid, tech in re.findall(r"<tr><td>(\d+)</td><td>[^<]*</td><td>(.*?)</td>", plans)}


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_适宜技术从下拉里选_首项不挂_选项写名称且转义(client, admin, world):
    form = _plan_form(_render(_responses(client, admin)))
    assert 'name="technique_id" type="number"' not in form   # 修前是手填编号的数字框
    select = re.search(r'<select name="technique_id">(.*?)</select>', form, re.S)
    assert select, form
    options = re.findall(r'<option value="([^"]*)">(.*?)</option>', select.group(1))
    assert options[0] == ("", "不挂适宜技术")
    techniques = world["techniques"]
    assert (str(techniques["P21430 艾灸足三里"]), "P21430 艾灸足三里") in options
    assert (str(techniques[NAME_PAYLOAD]), _esc(NAME_PAYLOAD)) in options
    assert "技术库没取到" not in form


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_计划表写适宜技术名称_没挂的写横杠_一律转义(client, admin, world):
    html = _render(_responses(client, admin))
    assert "<th>适宜技术</th>" in html   # 修前计划表没有这一列
    cells = _plan_cells(html)
    assert cells[world["plans"]["with"]["id"]] == _esc(NAME_PAYLOAD)
    assert cells[world["plans"]["without"]["id"]] == "—"


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_技术库没取到_下拉只剩不挂并说一句_整块照画(client, admin, world):
    html = _render(_responses(client, admin), broken="/api/tcm/techniques")
    form = _plan_form(html)
    select = re.search(r'<select name="technique_id">(.*?)</select>', form, re.S).group(1)
    assert re.findall(r'<option value="([^"]*)">(.*?)</option>', select) == [("", "不挂适宜技术")]
    assert "技术库没取到" in form
    assert _plan_cells(html)[world["plans"]["with"]["id"]] == _esc(NAME_PAYLOAD)   # 计划表照画


def test_下拉的选值仍按数字送_空的不送():
    """表单提交照旧走 formJson：technique_id 按数字送，选「不挂适宜技术」（空值）不送，后端按没挂收。"""
    draw = strip_comments(_function(_src("pages-public.js"), "async function drawEduGaps("))
    assert 'formJson(e.target, ["org_id", "technique_id", "capacity"])' in draw
    assert 'api("/api/tcm/techniques").catch(() => null)' in draw
