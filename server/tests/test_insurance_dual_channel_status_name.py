"""双通道状态文案是前端自带的一份，同页的特病队列取的是后端文案（P2-1482，第四十三批扫描 AG3-8）。

修前 `list_dual_channel` 的出参没有 `status_name`，页面用三元写死「已批准 / 已驳回 / 待审核」——表外的值一律显示成「待审核」
（后端哪天加了新状态，页面照样说「待审核」）；措辞与列注释（「通过 / 驳回」）也是两套。同一页的特病队列早就显示后端给的
`status_name`（P2-72）。`test_status_text_from_backend.py` 只认原样插值，看不出这种把三个值全译了的三元。

修法：后端加 `DUAL_CHANNEL_STATUS_NAMES`（措辞与同页特病同一套口径：已批准 / 已驳回，待审核照列注释），清单出参末尾只增
`status_name`、原有键与次序不动；页面改显示它，只保留配色；文案表登记进 `test_status_text_from_backend.py` 的对照名单。
"""
import json
import os
import re
import shutil
import subprocess

import pytest

from app.database import SessionLocal
from app.models import DualChannelApp
from conftest import login

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
ROW_KEYS = ["id", "patient_id", "drug_name", "reason", "status", "review_comment", "status_name"]


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


@pytest.fixture(scope="module")
def world(client, admin):
    county = client.post("/api/organizations", headers=admin, json={
        "name": "P21482 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    users = {}
    for username, role in (("p21482_doc", "doctor"), ("p21482_dir", "director")):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": role, "org_id": county})
        assert created.status_code == 201, created.text
        users[role] = created.json()["id"]
    doctor, director = login(client, "p21482_doc", "passw0rd1"), login(client, "p21482_dir", "passw0rd1")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21482 患者", "id_card": "330102197001011482"}).json()["id"]
    ids = {}
    for drug in ("待审", "批准", "驳回"):
        created = client.post("/api/insurance/dual-channel", headers=doctor, json={
            "patient_id": patient, "drug_name": f"P21482 {drug}药", "reason": "院内无药"})
        assert created.status_code == 201, created.text
        ids[drug] = created.json()["id"]
    for drug, approve in (("批准", "true"), ("驳回", "false")):
        reviewed = client.post(f"/api/insurance/dual-channel/{ids[drug]}/review?approve={approve}", headers=director)
        assert reviewed.status_code == 200, reviewed.text
    # 文案表之外的值（模拟后端哪天加了新状态、表还没跟上）：直接落库
    with SessionLocal() as db:
        row = DualChannelApp(patient_id=patient, drug_name="P21482 撤回药", status="withdrawn",
                             created_by=users["doctor"])
        db.add(row)
        db.commit()
        ids["表外"] = row.id
    return {"director": director, "ids": ids}


def test_清单出参末尾带状态文案_原有键与次序不动(client, world):
    rows = {r["id"]: r for r in client.get("/api/insurance/dual-channel", headers=world["director"]).json()}
    ids = world["ids"]
    assert all(list(r) == ROW_KEYS for r in rows.values())   # 修前没有 status_name
    assert [rows[ids[k]]["status_name"] for k in ("待审", "批准", "驳回", "表外")] == ["待审核", "已批准", "已驳回", "withdrawn"]
    pending = client.get("/api/insurance/dual-channel?status=pending", headers=world["director"]).json()
    assert [(r["id"], r["status_name"]) for r in pending] == [(ids["待审"], "待审核")]


#: `$()` 给记 innerHTML 的假元素，`api()` 经管道转给真接口；角色按管理层
_HARNESS = r"""
const els = {};
const mk = () => ({ textContent: "", innerHTML: "", value: "", style: {}, classList: { add() {}, remove() {} } });
globalThis.document = { addEventListener() {}, cookie: "", querySelector(sel) { return (els[sel] ||= mk()); } };
globalThis.localStorage = { getItem(key) { return key === "medplat_role" ? "director" : null; } };
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
async function api(path) {
  process.stdout.write(JSON.stringify({ get: path }) + "\n");
  return JSON.parse((await lines.next()).value);
}
function postAction() {}
function formJson() { return {}; }
function route() {}
async function spdModal() { return null; }
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_页面显示后端文案_表外的值不再说成待审核(client, world):
    core, clinical = _read("core.js"), _read("pages-clinical.js")
    script = (_HARNESS + _read("shared.js")
              + "".join(_top_level(core, f"function {name}(")
                        for name in ("table", "panel", "actionableFirst", "setMsg", "currentRole"))
              + _top_level(clinical, "async function renderInsurance(")
              + "\n(async () => { await renderInsurance();\n"
                "  process.stdout.write(JSON.stringify({ result: els['#page-body'].innerHTML }) + '\\n');\n"
                "  rl.close(); })();\n")
    proc = subprocess.Popen(["node", "-e", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                body = message["result"]
                break
            resp = client.get(message["get"], headers=world["director"])
            assert resp.status_code == 200, (message["get"], resp.text)
            proc.stdin.write(json.dumps(resp.json()) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
    start = body.index("<h3>双通道药品申报")
    table = body[start:body.index("</table>", start)]
    status = {cells[0]: cells[4] for cells in (re.findall(r"<td>(.*?)</td>", row, re.S)
                                               for row in re.findall(r"<tr>(.*?)</tr>", table, re.S)) if cells}
    ids = world["ids"]
    assert status[str(ids["待审"])] == '<span class="tag orange">待审核</span>'
    assert status[str(ids["批准"])] == '<span class="tag green">已批准</span>'
    assert status[str(ids["驳回"])] == '<span class="tag red">已驳回</span>'
    assert status[str(ids["表外"])] == '<span class="tag orange">withdrawn</span>'   # 修前：「待审核」


def test_页面不再三元写死三种文案_只保留配色():
    body = _top_level(_read("pages-clinical.js"), "async function renderInsurance(")
    code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("//"))
    assert '"待审核"' not in code and '"已批准"' not in code and '"已驳回"' not in code   # 修前三元里各一处
    assert '"orange"}">${esc(a.status_name)}</span></td>' in code   # 特病、双通道两张队列同一个写法
    assert code.count("${esc(a.status_name)}") == 2
