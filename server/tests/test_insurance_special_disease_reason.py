"""特病申报页面填不了申报理由，审核队列也不显示理由；同页双通道两样都有（P2-1481，第四十三批扫描 AG3-7）。

接口早就收 `reason`、出参也带（`insurance.SpecialDiseaseCreate` / `_special_disease_out`），修前页面的特病申报表单只有患者号
和病种、特病申报队列只有 ID / 患者 / 病种 / 状态——申报的人写不了理由，审核的人只看得见一个病种名。同一页的双通道表单收
理由、队列显示理由。修法照双通道：表单加「申报理由」，队列加理由列（空的写「—」）。这里把 `renderInsurance` 原样拿到 node
里跑，`api` 经管道转给真接口。
"""
import json
import os
import re
import shutil
import subprocess

import pytest

from conftest import login

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
REASON = "P21481 维持性血液透析 <每周三次>"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


#: `$()` 给记 innerHTML 的假元素，`api()` 经管道转给真接口；角色从参数来（表单只给申报方，审核按钮只给管理层）
_HARNESS = r"""
const els = {};
const mk = () => ({ textContent: "", innerHTML: "", value: "", style: {}, classList: { add() {}, remove() {} } });
globalThis.document = { addEventListener() {}, cookie: "", querySelector(sel) { return (els[sel] ||= mk()); } };
const ROLE = process.argv[1];
globalThis.localStorage = { getItem(key) { return key === "medplat_role" ? ROLE : null; } };
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


def _render(client, headers, role: str) -> str:
    core, clinical = _read("core.js"), _read("pages-clinical.js")
    script = (_HARNESS + _read("shared.js")
              + "".join(_top_level(core, f"function {name}(")
                        for name in ("table", "panel", "actionableFirst", "setMsg", "currentRole"))
              + _top_level(clinical, "async function renderInsurance(")
              + "\n(async () => { await renderInsurance();\n"
                "  process.stdout.write(JSON.stringify({ result: els['#page-body'].innerHTML }) + '\\n');\n"
                "  rl.close(); })();\n")
    proc = subprocess.Popen(["node", "-e", script, role], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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


@pytest.fixture(scope="module")
def world(client, admin):
    county = client.post("/api/organizations", headers=admin, json={
        "name": "P21481 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    for username, role in (("p21481_doc", "doctor"), ("p21481_dir", "director")):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": role, "org_id": county})
        assert created.status_code == 201, created.text
    doctor, director = login(client, "p21481_doc", "passw0rd1"), login(client, "p21481_dir", "passw0rd1")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21481 患者", "id_card": "330102197001011481"}).json()["id"]
    with_reason = client.post("/api/insurance/special-diseases", headers=doctor, json={
        "patient_id": patient, "disease_name": "P21481 尿毒症透析", "reason": REASON})
    without = client.post("/api/insurance/special-diseases", headers=doctor, json={
        "patient_id": patient, "disease_name": "P21481 恶性肿瘤门诊放化疗"})
    assert with_reason.status_code == without.status_code == 201, (with_reason.text, without.text)
    assert with_reason.json()["reason"] == REASON   # 接口一直收得下、出参也带
    return {"doctor": doctor, "director": director, "with_reason": with_reason.json()["id"],
            "without": without.json()["id"]}


def test_特病申报表单有申报理由一栏_照双通道的写法(client, world):
    body = _render(client, world["doctor"], "doctor")
    form = body[body.index('<form class="inline" id="spec-form">'):]
    form = form[:form.index("</form>")]
    # 修前只有患者号与病种两栏
    assert re.findall(r'<input name="(\w+)"', form) == ["patient_id", "disease_name", "reason"]
    assert '<input name="reason" placeholder="申报理由" style="min-width:180px">' in form
    dual = body[body.index('<form class="inline" id="dual-form">'):]
    assert '<input name="reason" placeholder="申报理由" style="min-width:180px">' in dual[:dual.index("</form>")]


def test_特病申报队列有理由列_空的写横杠_理由转义(client, world):
    body = _render(client, world["director"], "director")
    start = body.index("<h3>特病申报队列")
    table = body[start:body.index("</table>", start)]
    assert re.findall(r"<th>(.*?)</th>", table) == ["ID", "患者", "病种", "理由", "状态", "操作"]   # 修前没有「理由」
    rows = {cells[0]: cells for cells in (re.findall(r"<td>(.*?)</td>", row, re.S)
                                          for row in re.findall(r"<tr>(.*?)</tr>", table, re.S)) if cells}
    assert rows[str(world["with_reason"])][3] == "P21481 维持性血液透析 &lt;每周三次&gt;"
    assert rows[str(world["without"])][3] == "—"
