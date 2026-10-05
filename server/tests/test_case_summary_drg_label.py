"""住院页只读首页的 DRG 一行与病案首页打印件同一句话，由后端同一个帮手生成（P2-1536，第四十五批扫描 AI4-7）。

P2-1279 给打印件定了说法：QY 兜底组收的是哪组都没入上的病例，兜底与没入组同样印「未入组」，带上兜底组编码与复核提示
（「未入组（QY，需病案首页复核）」），免得 QY 被当成一个权重 0.5 的组；正式入组的印「编码（权重 x）」。住院页点「病案首页」
给的只读首页（`pages-clinical.js::renderInpatient`，P2-617）却自己写了一份 `DRG：${drg_code || "未入组"}`——修前实测：同一份
首页，兜底病例页面上印「DRG：QY」，打印出来是「未入组（QY，需病案首页复核）」；正式入组的页面上只有编码、没有权重。

修法：打印件里那句的判断抽成 `routers/drgs.py::drg_label`（DRG 模块自己的口径；打印件本就 import 兄弟路由的文案帮手，drgs
不 import 打印件与住院，不成环），打印件改调它、字节不变；`GET /api/inpatient/admissions/{id}/case-summary` 出参末尾只增一个
`drg_label`，由同一个帮手生成（结案回执 POST 不动）；页面只读首页印这个键，不在页面上再写一份判断。
"""
import inspect
import json
import os
import re
import shutil
import subprocess

import pytest

from app.data.drg_groups_seed import FALLBACK_DRG_GROUP

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")

#: GET 回读原有的 11 键（契约网 test_inpatient_contract.py 的 CASE_KEYS），新键只许追加在末尾
CASE_KEYS = [
    "id", "admission_id", "discharge_diagnosis", "operation", "total_cost",
    "drug_cost", "outcome", "note", "drg_code", "drg_weight", "created_by_name",
]


@pytest.fixture(scope="module")
def world(client, admin):
    """三种首页：落 QY 兜底组的、正式入组的、没入组的（drg_code 空：入组上线前或入组那一步没跑的存量，直接落库）。"""
    from app.database import SessionLocal
    from app.models import CaseSummary

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21536 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21536 内科"}).json()["id"]
    out = {}
    for i, (key, diagnosis) in enumerate((("fallback", "高钾血症"), ("grouped", "脑梗死"), ("ungrouped", "肺炎"))):
        bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": f"0{i + 1}"}).json()["id"]
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P21536 患者{i}", "id_card": f"33012719550101{1536 + i:04d}"}).json()["id"]
        adm = client.post("/api/inpatient/admissions", headers=admin, json={
            "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": diagnosis})
        assert adm.status_code == 201, adm.text
        aid = adm.json()["id"]
        if key == "ungrouped":
            with SessionLocal() as db:
                db.add(CaseSummary(admission_id=aid, discharge_diagnosis=diagnosis, total_cost=3000, drug_cost=800,
                                   outcome="好转", created_by_name="P21536 病案室"))
                db.commit()
            out[key] = {"admission": aid, "drg_code": "", "drg_weight": 0.0}
            continue
        resp = client.post(f"/api/inpatient/admissions/{aid}/case-summary", headers=admin, json={
            "discharge_diagnosis": diagnosis, "total_cost": 3000, "drug_cost": 800})
        assert resp.status_code == 201, resp.text
        out[key] = {"admission": aid, "drg_code": resp.json()["drg_code"], "drg_weight": resp.json()["drg_weight"],
                    "receipt_keys": list(resp.json())}
    assert out["fallback"]["drg_code"] == FALLBACK_DRG_GROUP["code"]
    assert out["grouped"]["drg_code"] not in ("", FALLBACK_DRG_GROUP["code"])
    return out


def _print_cell(client, admin, admission_id: int) -> str:
    resp = client.get(f"/api/print/case-summaries/{admission_id}", headers=admin)
    assert resp.status_code == 200, resp.text
    cells = re.findall(r'<td class="k">DRG 分组</td><td>([^<]*)</td>', resp.text)
    assert len(cells) == 1, resp.text   # 防空转：确实取到了「DRG 分组」那一格
    return cells[0]


def _detail(client, admin, admission_id: int) -> dict:
    resp = client.get(f"/api/inpatient/admissions/{admission_id}/case-summary", headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize("key, expected", [
    ("fallback", "未入组（QY，需病案首页复核）"),
    ("grouped", None),   # 「编码（权重 x）」：编码与权重取这一例入组时的快照
    ("ungrouped", "未入组"),
])
def test_三种首页_接口回的drg_label与打印件那一格逐字相同(client, admin, world, key, expected):
    case = world[key]
    if expected is None:
        expected = f"{case['drg_code']}（权重 {case['drg_weight']}）"
    body = _detail(client, admin, case["admission"])
    assert body.get("drg_label") == expected, body   # 修前接口没有这个键，页面自己写成「DRG：QY」
    assert _print_cell(client, admin, case["admission"]) == expected   # 打印件字节不变（P2-1279 的说法）


def test_回读出参只在末尾多一个drg_label_原有11键与次序不动(client, admin, world):
    body = _detail(client, admin, world["fallback"]["admission"])
    assert list(body) == CASE_KEYS + ["drg_label"]
    # 结案回执（POST）不在本条：照旧 11 键加尾键 drg（入组结果），不多 drg_label
    assert world["grouped"]["receipt_keys"] == world["fallback"]["receipt_keys"] == CASE_KEYS + ["drg"]


def test_一句话只有一个产地_打印件与回读都调drg_label():
    from app.routers import drgs, inpatient, printing

    assert drgs.drg_label("", 0.0) == "未入组"
    assert drgs.drg_label(FALLBACK_DRG_GROUP["code"], 0.5) == "未入组（QY，需病案首页复核）"
    assert drgs.drg_label("ES31", 0.95) == "ES31（权重 0.95）"
    printed = inspect.getsource(printing.print_case_summary)
    assert "drg_label(summary.drg_code, summary.drg_weight)" in printed
    assert "需病案首页复核" not in printed   # 不再各写一份
    assert "drg_label(summary.drg_code, summary.drg_weight)" in inspect.getsource(inpatient.get_case_summary)


# ---------------------------------------------------------------- 页面：只读首页印 drg_label

pytest_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


#: 页面取数换成桩（写法照 test_inpatient_discharged_case_summary_entry 的 `_HARNESS`）：`api()` 经管道转给真接口，失败照 core.js
#: 的 `api` 抛 `errorText` 的话、带上 `status`；`spdModal()` 记下标题、说明与字段，点了就交回空对象
_HARNESS = r"""
const args = JSON.parse(process.argv[1]);
const els = {};
const mk = () => ({ textContent: "", innerHTML: "", value: "", className: "", dataset: {},
  classList: { add() {}, remove() {} } });
globalThis.document = { addEventListener() {}, cookie: "", querySelector(sel) { return (els[sel] ||= mk()); } };
globalThis.localStorage = { getItem() { return null; }, setItem() {} };
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
async function api(path, opts = {}) {
  const method = opts.method || "GET";
  const body = opts.body ? JSON.parse(opts.body) : null;
  process.stdout.write(JSON.stringify({ req: { method, path, body } }) + "\n");
  const resp = JSON.parse((await lines.next()).value);
  if (resp.status >= 400) {
    const err = new Error(errorText(resp.body.detail, `请求失败(${resp.status})`));
    err.status = resp.status;
    throw err;
  }
  return resp.body;
}
function route() {}
function setMsg() {}
function formJson() { return {}; }
async function postAction() {}
async function openPrintPage() {}
let modal = null;
async function spdModal(title, fields, opts = {}) {
  modal = { title, intro: opts.intro || "", fields: fields.map((f) => ({ name: f.name })) };
  return {};
}
"""

_RUN = r"""
(async () => {
  await renderInpatient();
  const body = els["#page-body"].innerHTML;
  if (!body.includes(`data-summary="${args.aid}"`)) throw new Error("住院记录表里没有这一行的「病案首页」按钮");
  await els["#page-body"].onclick({ target: { dataset: { summary: String(args.aid) } } });
  process.stdout.write(JSON.stringify({ result: { modal } }) + "\n");
  rl.close();
})().catch((err) => {
  process.stdout.write(JSON.stringify({ error: String((err && err.stack) || err) }) + "\n");
  rl.close();
});
"""


def _read_only_summary(client, headers, aid: int) -> dict:
    """渲染住院页、点住院 `aid` 那一行的「病案首页」，回只读首页那张框。"""
    core, page = _read("core.js"), _read("pages-clinical.js")
    script = (_HARNESS + _read("shared.js")
              + "".join(_top_level(core, head) for head in ("function table(", "function panel("))
              + _top_level(page, "async function renderInpatient(") + _RUN)
    proc = subprocess.Popen(["node", "-e", script, json.dumps({"aid": aid})],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]["modal"]
            assert "error" not in message, message["error"]
            req = message["req"]
            resp = client.request(req["method"], req["path"], headers=headers, json=req["body"])
            proc.stdin.write(json.dumps({"status": resp.status_code, "body": resp.json()}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


@pytest_node
@pytest.mark.parametrize("key", ["fallback", "grouped", "ungrouped"])
def test_住院页只读首页的DRG一行印接口给的那句(client, admin, world, key):
    case = world[key]
    modal = _read_only_summary(client, admin, case["admission"])
    assert modal is not None and "已填写" in modal["title"] and modal["fields"] == [], modal
    lines = modal["intro"].split("\n")
    drg_lines = [line for line in lines if line.startswith("DRG")]
    # 修前兜底病例印「DRG：QY」、正式入组的只有编码；现在与打印件「DRG 分组」那一格同一句
    assert drg_lines == [f"DRG 分组：{_print_cell(client, admin, case['admission'])}"], lines


def test_页面不再自己判断未入组():
    page = _read("pages-clinical.js")
    body = page[page.index("async function renderInpatient("):page.index("\nasync function ", page.index(
        "async function renderInpatient(") + 1)]
    assert "filled.drg_label" in body
    assert "filled.drg_code" not in body   # 修前 `DRG：${filled.drg_code || "未入组"}`：状态文案在页面上另写一份
