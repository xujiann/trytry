"""互认预检对危急值源报告只带结论、不带危急标记，页面默认选中「互认」（P2-1709，第五十批扫描 AN2-1）。

`GET /api/exams/recognition-check` 的可互认分支原先只出 `recognizable / request_id / item_name / conclusion`：甲乡 20 天前开的
血钾出了危急值（「血钾 2.6 mmol/L，低钾血症」），甲乡至今未确认；乙乡给同一患者开血钾时预检照样 `recognizable: true`，开单页
（`core.js renderExams` 的开单框）弹「可互认」框、处理方式**默认选中「互认该结果，不再重复检查」**，框头只印结论——照默认点确定，
建单 201 即成「已互认」，乙乡的待办、站内消息里没有任何危急值提示（修前实测：预检
`{'recognizable': True, 'request_id': 1, 'item_name': '血钾', 'conclusion': '血钾 2.6 mmol/L，低钾血症'}`）。

修法（不需裁定的那一半）：预检出参**末尾只增** `critical`（源报告是否危急值）与 `critical_status`（闭环到哪一步，取不到为空串），
原有键与次序不动；开单框在源报告是危急值时写明「该结果为危急值（当前状态：…）」，处理方式默认改成「不互认」，不是危急值的照旧
默认互认。危急值报告要不要排除出互认、建单时要不要通知互认方、互认时效要不要按项目设，都另行裁定，这里不做。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
CHECK_KEYS = ["recognizable", "request_id", "item_name", "conclusion", "critical", "critical_status"]


@pytest.fixture(scope="module")
def world(client, admin):
    """甲乡开的血钾出了危急值（未确认），甲乡开的血常规出了普通报告；乙乡给同一患者再开这两项。"""
    town_a, town_b = (client.post("/api/organizations", headers=admin, json={
        "name": name, "org_type": "township", "level": "township"}).json()["id"] for name in ("P21709 甲乡", "P21709 乙乡"))
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21709 患者", "id_card": "330106196505051709", "gender": "男"})
    assert patient.status_code == 201, patient.text
    pid = patient.json()["id"]

    def reported(item_code, item_name, conclusion, critical):
        req = client.post("/api/exams", headers=admin, json={
            "patient_id": pid, "from_org_id": town_a, "center_type": "lab", "item_code": item_code, "item_name": item_name})
        assert req.status_code == 201, req.text
        assert client.post(f"/api/exams/{req.json()['id']}/claim", headers=admin).status_code == 200
        rep = client.post(f"/api/exams/{req.json()['id']}/report", headers=admin, json={
            "conclusion": conclusion, "critical": critical})
        assert rep.status_code == 201, rep.text
        return req.json()["id"], rep.json()["id"]

    k_req, k_rep = reported("P21709-K", "血钾", "血钾 2.6 mmol/L，低钾血症", True)
    cbc_req, _ = reported("P21709-CBC", "血常规", "血常规未见异常", False)
    return {"patient": pid, "town_b": town_b, "k_req": k_req, "k_rep": k_rep, "cbc_req": cbc_req}


def _check(client, admin, world, item_code):
    resp = client.get("/api/exams/recognition-check", headers=admin, params={
        "patient_id": world["patient"], "item_code": item_code, "center_type": "lab", "from_org_id": world["town_b"]})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_危急值源报告_预检带危急标记与闭环状态_键在末尾(client, admin, world):
    body = _check(client, admin, world, "P21709-K")
    assert list(body) == CHECK_KEYS, body   # 修前只有前四键：不带危急标记
    assert body["recognizable"] is True and body["request_id"] == world["k_req"]
    assert body["critical"] is True and body["critical_status"] == "notified"
    # 闭环往前走一步，预检报的是库里此刻的状态
    assert client.post(f"/api/exams/reports/{world['k_rep']}/acknowledge", headers=admin).status_code == 200
    assert _check(client, admin, world, "P21709-K")["critical_status"] == "acknowledged"


def test_非危急源报告_危急标记为假_状态空串(client, admin, world):
    body = _check(client, admin, world, "P21709-CBC")
    assert list(body) == CHECK_KEYS, body
    assert body["request_id"] == world["cbc_req"]
    assert body["critical"] is False and body["critical_status"] == ""


def test_无可互认报告分支不多出新键(client, admin, world):
    """条件键的另一半：新键只在可互认分支出现，别的分支照旧整个不在（不是 null）。"""
    assert _check(client, admin, world, "P21709-NONE") == {"recognizable": False}


# ---------- 开单框（core.js renderExams 的开单表单） ----------


def _top_level(source: str, head: str, end: str = "\n}\n") -> str:
    start = source.index(head)
    return source[start:source.index(end, start) + len(end)]


def _const(source: str, name: str) -> str:
    start = source.index(f"const {name} = ")
    return source[start:source.index(";\n", start) + 2]


#: 页面取数换成桩：清单一律空，预检回给定的那份；`spdModal` 记下框头与各字段的缺省值，当作点了取消
_HARNESS = """
const els = {};
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", innerHTML: "", className: "", value: "", dataset: {},
    classList: { add() {}, remove() {} }, addEventListener() {} }); } };
globalThis.FormData = class { constructor(form) { this.values = form.values; } get(k) { return this.values[k] ?? null; } };
const CHECK = JSON.parse(process.argv[1]);
const posted = [];
async function api(path, opts = {}) {
  if (path.startsWith("/api/exams/recognition-check")) return CHECK;
  if (opts.method === "POST") { posted.push(path); return {}; }
  return [];
}
const modals = [];
async function spdModal(title, fields, opts = {}) {
  modals.push({ title, intro: opts.intro || "", values: Object.fromEntries(fields.map((f) => [f.name, f.value ?? null])) });
  return null;
}
const route = () => {}; const pollTodos = () => {};
"""


def _open_modal(check: dict) -> dict:
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    clinical = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    script = (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8")
              + _top_level(core, "function table(") + _top_level(core, "function panel(")
              + _top_level(core, "function actionableFirst(") + _top_level(core, "function setMsg(")
              + "".join(_const(core, name) for name in ("CENTER_NAMES", "EXAM_STATUS", "SAMPLE_NEXT", "SAMPLE_STATUS"))
              + _const(clinical, "CRIT_STATUS") + _top_level(core, "async function renderExams()")
              + "(async () => { await renderExams();\n"
              "  await $('#exam-form').onsubmit({ preventDefault() {}, target: { values: { patient_id: '1',"
              " from_org_id: '2', center_type: 'lab', item_code: 'K', item_name: '血钾', clinical_info: '' } } });\n"
              "  process.stdout.write(JSON.stringify({ modals, posted, msg: $('#exam-msg').textContent })); })();\n")
    out = subprocess.run(["node", "-e", script, json.dumps(check, ensure_ascii=False)], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)
    assert len(result["modals"]) == 1 and result["posted"] == [], result   # 弹了互认框，取消即不开单
    return result["modals"][0]


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_开单框_危急值源报告写明并默认不互认(client, admin, world):
    check = _check(client, admin, world, "P21709-K")   # 上面已确认接收：已确认、还没处置反馈
    modal = _open_modal(check)
    assert modal["values"]["decision"] == "decline", modal   # 修前缺省「accept」：照默认点确定即成「已互认」
    assert "该结果为危急值（当前状态：已确认）" in modal["intro"], modal   # 修前框头只印结论
    assert "血钾 2.6 mmol/L，低钾血症" in modal["intro"]


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_开单框_存量危急值空状态按已通知写(client, admin, world):
    check = {**_check(client, admin, world, "P21709-K"), "critical_status": ""}   # 迁移前的存量危急报告
    modal = _open_modal(check)
    assert modal["values"]["decision"] == "decline"
    assert "该结果为危急值（当前状态：已通知）" in modal["intro"], modal


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_开单框_非危急源报告照旧默认互认_不写危急(client, admin, world):
    modal = _open_modal(_check(client, admin, world, "P21709-CBC"))
    assert modal["values"]["decision"] == "accept", modal
    assert "危急值" not in modal["intro"], modal
