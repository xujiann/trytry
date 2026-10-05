"""项目页的逾期老项目在清单里找不到：卡片「逾期未结 N」算着，清单只有最新 200 个（P2-1441，第四十二批扫描 AF3-11，补充 P2-414）。

修前 `renderProjects` 只取不带参数的 `/api/projects`——接口按立项倒序只回最新 200 个（`projects.list_projects` 的 `limit(200)`），
P2-414 修好的 `overdue_only`（在库里筛、再截前 200）页面一处都没用。扫描实测：卡片「项目总数 201、逾期未结 1」，页面 200 行
里标逾期的是 0 行——逾期最久的那个老项目恰恰被后来立项的挤出窗口，「报进度」也够不着；标题照样只写「项目清单」。

修法同 P2-1310：另取一遍 `?overdue_only=true`，`actionableFirst` 排在最前、按 id 去重；列出的比卡片的「项目总数」少时，标题
写明「共 N 个：逾期未结 M 个排在最前、其余只列最新 K 个」。这里把 `renderProjects` 原样拿到 node 里跑，`api` 经管道转给真接口。
"""
import json
import os
import re
import shutil
import subprocess

import pytest

from app.database import SessionLocal
from app.models import AdminProject

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
OLD = "P21441 老院区改造（早该完工）"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def _const(source: str, name: str) -> str:
    return re.search(rf"^const {name} = .*?;\n", source, re.M | re.S).group(0)


#: 项目页取数换成桩：`$()` 给记 innerHTML 的假元素，`api()` 经管道转给真接口
_HARNESS = r"""
const els = {};
const mk = () => ({ textContent: "", innerHTML: "", value: "", style: {}, classList: { add() {}, remove() {} } });
globalThis.document = { addEventListener() {}, cookie: "", querySelector(sel) { return (els[sel] ||= mk()); } };
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const requested = [];
async function api(path) {
  requested.push(path);
  process.stdout.write(JSON.stringify({ get: path }) + "\n");
  return JSON.parse((await lines.next()).value);
}
function postAction() {}
function formJson() { return {}; }
function route() {}
async function spdModal() { return null; }
"""


def _render(client, headers) -> dict:
    core, clinical = _read("core.js"), _read("pages-clinical.js")
    script = (_HARNESS + _read("shared.js")
              + "".join(_top_level(core, f"function {name}(") for name in ("table", "panel", "actionableFirst", "setMsg"))
              + _const(clinical, "PROJECT_STATUS_OPTS") + _const(clinical, "MS_STATUS")
              + _top_level(clinical, "async function renderProjects(")
              + "\n(async () => { await renderProjects();\n"
                "  process.stdout.write(JSON.stringify({ result: { body: els['#page-body'].innerHTML, requested } }) + '\\n');\n"
                "  rl.close(); })();\n")
    proc = subprocess.Popen(["node", "-e", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            resp = client.get(message["get"], headers=headers)
            assert resp.status_code == 200, (message["get"], resp.text)
            proc.stdin.write(json.dumps(resp.json()) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def _project_panel(body: str) -> tuple[str, list[list[str]]]:
    """项目清单面板：(标题, [每行的各格])。"""
    start = body.index("<h3>项目清单")
    title = body[start + len("<h3>"):body.index("</h3>", start)]
    table = body[start:body.index("</table>", start)]
    rows = [re.findall(r"<td>(.*?)</td>", row, re.S) for row in re.findall(r"<tr>(.*?)</tr>", table, re.S)]
    return title, [cells for cells in rows if cells]


@pytest.fixture(scope="module")
def org(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21441 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    with SessionLocal() as db:   # 最早立项、早该完工的那个，之后又立了 200 个还没到期的（逐个走接口太慢，直接落库）
        db.add(AdminProject(org_id=org, name=OLD, status="ongoing", start_date="2024-01-01", due_date="2025-06-30"))
        db.flush()
        db.add_all([AdminProject(org_id=org, name=f"P21441 新项目{i}", status="ongoing", due_date="2099-12-31")
                    for i in range(200)])
        db.commit()
    return org


def test_201个项目_最老那个逾期的在页面数据里_排在最前_标逾期(client, admin, org):
    stats = client.get("/api/projects/stats/overview", headers=admin).json()
    assert (stats["total"], stats["overdue"]) == (201, 1)
    assert OLD not in [p["name"] for p in client.get("/api/projects", headers=admin).json()]   # 缺省一页取不到它

    out = _render(client, admin)
    title, rows = _project_panel(out["body"])
    names = [cells[0] for cells in rows]
    assert names[0] == f'{OLD} <span class="tag danger">逾期</span>', names[:3]   # 修前 200 行里没有它、标逾期的 0 行
    assert sum('<span class="tag danger">逾期</span>' in name for name in names) == stats["overdue"]
    assert len(rows) == 201 and title == "项目清单"   # 逾期的补进来正好列全，标题不用写截断
    assert "/api/projects?overdue_only=true" in out["requested"]


def test_列不全时标题写明截断_数与页面行数一致(client, admin, org):
    created = client.post("/api/projects", headers=admin, json={"org_id": org, "name": "P21441 又一个新项目",
                                                                "due_date": "2099-12-31"})
    assert created.status_code == 201, created.text
    title, rows = _project_panel(_render(client, admin)["body"])
    # 共 202 个：最新一页 200 个 + 逾期的那个，最早立项、没逾期的新项目0 不在页面上
    assert title == "项目清单（共 202 个：逾期未结 1 个排在最前、其余只列最新 200 个）"
    assert rows[0][0].startswith(OLD) and len(rows) == 201
    assert "P21441 新项目0" not in [cells[0] for cells in rows]
