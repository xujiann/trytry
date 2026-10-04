"""制剂批次表与效期预警的「配方 / 制剂」列显示配方名（P2-1411，第四十一批扫描 AE4-10）。

修前两张表这一列印的是 `formula_id` 数字（`<td>${b.formula_id}</td>`）——同一个面板上面已经取到了配方清单，药剂科看批次、看
效期预警还得拿编号回配方表对。修后按本页已取到的配方清单映射出配方名，映射不到的（配方清单只回最新 200 个）回显编号，一律
`esc()`；接口不动（出参不加键）。
"""
import json
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import business_today, login

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
PUBLIC = (STATIC / "pages-public.js").read_text(encoding="utf-8")
#: 制剂名里夹一段标签：显示时必须转义
NAME_PAYLOAD = "P21411 清热合剂<b>x</b>"


def _days(n: int) -> str:
    return (business_today() + timedelta(days=n)).isoformat()


def _function(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 2]


def _draw() -> str:
    return _function(PUBLIC, "async function drawTcmPreparations(")


@pytest.fixture(scope="module")
def world(client, admin):
    """两个配方、三个批次：益气颗粒一批在产（半年效期）、一批十天后到期；清热合剂一批已过期。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21411 中医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    resp = client.post("/api/users", headers=admin, json={
        "username": "p21411_pha", "password": "pw123456", "role": "pharmacist", "org_id": org})
    assert resp.status_code in (200, 201), resp.text
    pha = login(client, "p21411_pha", "pw123456")
    formulas = {}
    for code, name in (("P21411-1", "P21411 益气颗粒"), ("P21411-2", NAME_PAYLOAD)):
        resp = client.post("/api/tcm/formulas", headers=pha, json={"code": code, "name": name, "shelf_life_months": 6})
        assert resp.status_code == 201, resp.text
        formulas[code] = resp.json()["id"]
    batches = {}
    for batch_no, code, produced, expire in (("P21411-B1", "P21411-1", _days(-10), ""),
                                             ("P21411-B2", "P21411-1", _days(-100), _days(10)),
                                             ("P21411-B3", "P21411-2", _days(-400), "")):
        resp = client.post("/api/tcm/preparation-batches", headers=pha, json={
            "formula_id": formulas[code], "batch_no": batch_no, "org_id": org, "quantity": 10,
            "produced_date": produced, "expire_date": expire})
        assert resp.status_code == 201, resp.text
        batches[batch_no] = resp.json()
    return {"formulas": formulas, "batches": batches}


def _responses(client, admin) -> dict:
    responses = {}
    for path in re.findall(r'\bapi\("([^"]+)"\)', _draw()):
        resp = client.get(path, headers=admin)
        assert resp.status_code == 200, (path, resp.text)
        responses[path] = resp.json()
    return responses


def _render(responses: dict) -> str:
    """把 `drawTcmPreparations` 原样拿到 node 里跑：`api` 回接口原样的响应，`appendSection` 收下它画出的整块 HTML。"""
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    forms = re.search(r"^const DOSAGE_FORMS = .*;$", PUBLIC, re.M).group(0)
    script = (
        "globalThis.document = { addEventListener() {}, querySelector() { return null; }, cookie: '' };\n"
        + (STATIC / "shared.js").read_text(encoding="utf-8")
        + _function(core, "function table(") + _function(core, "function panel(")
        + _function(core, "function actionableFirst(") + forms + "\n"
        + "const RESPONSES = JSON.parse(process.argv[1]);\n"
        "async function api(path) {\n"
        "  if (!(path in RESPONSES)) throw new Error(`没料到的请求：${path}`);\n"
        "  return JSON.parse(JSON.stringify(RESPONSES[path]));\n"
        "}\n"
        "let drawn = '';\n"
        "function appendSection(html) { drawn += html; return { querySelector() { return {}; } }; }\n"
        + _draw()
        + "\n(async () => { await drawTcmPreparations(); process.stdout.write(drawn); })();\n"
    )
    done = subprocess.run(["node", "-e", script, json.dumps(responses, ensure_ascii=False)],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _formula_cells(html: str) -> dict:
    """`{(哪张表, 批号): 配方列}`：效期预警行是「批号 / 制剂 / …」，批次表行是「ID / 批号 / 配方 / …」。"""
    warn = html[html.index("制剂效期预警"):html.index("制剂批次（")]
    table = html[html.index("制剂批次（"):]
    cells = {}
    for batch_no, formula in re.findall(r"<tr><td>(P21411-B\d)</td><td>(.*?)</td>", warn):
        cells[("预警", batch_no)] = formula
    for batch_no, formula in re.findall(r"<tr><td>\d+</td><td>(P21411-B\d)</td><td>(.*?)</td>", table):
        cells[("批次", batch_no)] = formula
    return cells


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_批次表与效期预警的配方列显示配方名_一律转义(client, admin, world):
    cells = _formula_cells(_render(_responses(client, admin)))
    escaped = "P21411 清热合剂&lt;b&gt;x&lt;/b&gt;"
    assert cells == {
        ("预警", "P21411-B2"): "P21411 益气颗粒",   # 修前印配方编号
        ("预警", "P21411-B3"): escaped,
        ("批次", "P21411-B1"): "P21411 益气颗粒",
        ("批次", "P21411-B2"): "P21411 益气颗粒",
        ("批次", "P21411-B3"): escaped,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_映射不到的配方回显编号(client, admin, world):
    """配方清单只回最新 200 个：更早的配方映射不到，照旧显示编号，不显示空白。"""
    responses = _responses(client, admin)
    dropped = world["formulas"]["P21411-2"]
    responses["/api/tcm/formulas"] = [f for f in responses["/api/tcm/formulas"] if f["id"] != dropped]
    cells = _formula_cells(_render(responses))
    assert cells[("预警", "P21411-B3")] == cells[("批次", "P21411-B3")] == str(dropped)
    assert cells[("批次", "P21411-B1")] == "P21411 益气颗粒"


def test_两张表的配方列取名称_不再印编号():
    draw = _draw()
    assert draw.count("<td>${esc(formulaOf(b))}</td>") == 2
    assert "<td>${b.formula_id}</td>" not in draw   # 修前两处都是它
