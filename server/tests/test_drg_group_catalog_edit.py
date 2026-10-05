"""DRG 分组目录页补「编辑」与「停用 / 启用」：关键词、主手术关键词、必须命中主手术、MDC、名称、启停都改得了（P2-1535，第四十五批扫描 AI4-6）。

`PATCH /api/drgs/groups/{id}` 收名称、权重、主诊断 / 主手术关键词、必须命中主手术、MDC、MDC 名称与启停（`DrgGroupUpdate`，
动了匹配配置的按 P2-1019 与存量合并后判「永远入不了」），DRG 页（`pages-public.js::renderDrgs`）的分组目录却只给「调权」，
页面唯一的改动调用是 PATCH `{base_weight}`；「状态」列只显示、不给切换。修前实测（node 渲染页面）：目录每行只有「调权」，
点 `data-drg-edit` / `data-drg-toggle` 什么也不发生——建错的组（关键词过宽、漏勾必须命中主手术）停不掉也改不了，继续吃病例；
种子只增不改，存量库的关键词只能调接口改。

修法：管理员（与建组、调权同一个 `canGroup`）在普通组的行上多「编辑」与「停用 / 启用」：
- 编辑：页内表单（`spdModal`）列出名称、基准权重、MDC、MDC 名称、主诊断 / 主手术关键词、必须命中主手术，预填原值；只送和预填值
  不同的项（照 P2-969 / P2-1533），一项都没变不发请求；启停用行上的按钮，只改权重的「调权」照旧；失败把后端的话写进 `#drg-msg`；
- 启停：照同文件 ESB 接入方、数据质控规则的切换写法，送 `{active: !现状}`；
- 兜底组（`is_fallback`）不摆这两个：入组时它按编码取、不看启停，关键词对它也没有意义。「调权」照旧。

页面函数原样拿到 node 里跑（写法照 test_org_group_page_edit 的 `_HARNESS`），`api` 经管道转给真接口；`spdModal` 换成桩：
记下框里的字段与预填值，按用例给的改动交回（与 spdModal 一样去首尾空白），`answer` 为 None 即点了取消。点击只点页面上真摆出来
的按钮：从渲染出的按钮标签里取 `data-*` 当 `dataset`。
"""
import json
import os
import shutil
import subprocess

import pytest

from conftest import login

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


#: 页面取数换成桩：`$()` 按选择器给一个记 innerHTML 的假元素；`localStorage` 只回 `args.role`（`currentRole()` 读它）；`api()` 经
#: 标准输出把请求交给测试进程、从标准输入读回真接口的状态码与响应（失败照 core.js 的 `api` 抛 `errorText` 的话）；`spdModal()`
#: 记下字段、按 `args.answer` 改几项交回；`setMsg()` / `route()` 记下调用
_HARNESS = r"""
const args = JSON.parse(process.argv[1]);
const els = {};
const mk = () => ({ textContent: "", innerHTML: "", value: "", className: "", dataset: {},
  classList: { add() {}, remove() {} } });
globalThis.document = { addEventListener() {}, cookie: "", querySelector(sel) { return (els[sel] ||= mk()); } };
globalThis.localStorage = { getItem(key) { return key === "medplat_role" ? args.role : null; }, setItem() {} };
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const calls = [];
async function api(path, opts = {}) {
  const method = opts.method || "GET";
  const body = opts.body ? JSON.parse(opts.body) : null;
  calls.push([method, path, body]);
  process.stdout.write(JSON.stringify({ req: { method, path, body } }) + "\n");
  const resp = JSON.parse((await lines.next()).value);
  if (resp.status >= 400) {
    const err = new Error(errorText(resp.body.detail, `请求失败(${resp.status})`));
    err.status = resp.status;
    throw err;
  }
  return resp.body;
}
let routed = 0;
function route() { routed += 1; }
const msgs = [];
function setMsg(sel, text, ok = true) { msgs.push([sel, text, ok]); }
function formJson() { return {}; }
async function postAction() {}
let modal = null;
async function spdModal(title, fields, opts = {}) {
  modal = { title, intro: opts.intro || "", fields: fields.map((f) => ({ ...f })) };
  if (args.answer === null) return null;
  const out = {};
  for (const f of fields) {
    const raw = String(f.name in args.answer ? args.answer[f.name] : (f.value ?? "")).trim();
    out[f.name] = f.type === "number" ? Number(raw || 0) : raw;
  }
  return out;
}
// 页面上那颗按钮的 data-* 收成 dataset（与浏览器同样把 data-drg-edit 记成 drgEdit）；页面上没摆就是 null
function datasetOf(body, marker) {
  const at = body.indexOf(marker);
  if (at < 0) return null;
  const tag = body.slice(body.lastIndexOf("<button", at), body.indexOf(">", at));
  const ds = {};
  for (const m of tag.matchAll(/data-([\w-]+)="([^"]*)"/g)) ds[m[1].replace(/-(\w)/g, (_, c) => c.toUpperCase())] = m[2];
  return ds;
}
"""

_RUN = r"""
(async () => {
  await renderDrgs();
  const body = els["#page-body"].innerHTML;
  const dataset = args.button ? datasetOf(body, args.button) : null;
  if (dataset) await els["#page-body"].onclick({ target: { dataset } });
  process.stdout.write(JSON.stringify({ result: { body, clicked: dataset, modal, calls, msgs, routed } }) + "\n");
  rl.close();
})().catch((err) => {
  process.stdout.write(JSON.stringify({ error: String((err && err.stack) || err) }) + "\n");
  rl.close();
});
"""


def _run_page(client, headers, role: str = "admin", button: str = "", answer: dict | None = None) -> dict:
    """以 `role` 渲染 DRG 页；给了 `button`（如 `data-drg-edit="5"`）就点页面上那颗按钮，框里按 `answer` 改（None 即取消）。"""
    core, page = _read("core.js"), _read("pages-public.js")
    script = (_HARNESS + _read("shared.js")
              + "".join(_top_level(core, head) for head in (
                  "function table(", "function panel(", "function currentRole(", "function barChart("))
              + _top_level(page, "async function renderDrgs(") + _RUN)
    proc = subprocess.Popen(["node", "-e", script, json.dumps({"role": role, "button": button, "answer": answer},
                                                             ensure_ascii=False)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            assert "error" not in message, message["error"]
            req = message["req"]
            resp = client.request(req["method"], req["path"], headers=headers, json=req["body"])
            proc.stdin.write(json.dumps({"status": resp.status_code, "body": resp.json()}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def _writes(out: dict) -> list:
    return [call for call in out["calls"] if call[0] != "GET"]


def _row(body: str, gid: int) -> str:
    """分组目录里分组 `gid` 那一行（按「调权」按钮认：管理员每行都有它）。"""
    at = body.index(f'data-drg-weight="{gid}"')
    return body[body.rindex("<tr>", 0, at):body.index("</tr>", at)]


def _group(client, admin, code: str, **extra) -> dict:
    payload = {"code": code, "name": f"P21535 {code} 试验组", "base_weight": 1.1, "keywords": f"P21535{code}病",
               "mdc": "MDCZ", "mdc_name": "P21535 试验大类", **extra}
    resp = client.post("/api/drgs/groups", headers=admin, json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _saved(client, admin, gid: int) -> dict:
    return next(g for g in client.get("/api/drgs/groups", headers=admin).json() if g["id"] == gid)


def _candidates(client, admin, diagnosis: str, operation: str = "") -> list[str]:
    resp = client.post("/api/drgs/pre-check", headers=admin, json={"diagnosis": diagnosis, "operation": operation})
    assert resp.status_code == 200, resp.text
    return [c["code"] for c in resp.json()["candidates"]]


def _grouped_as(client, admin, diagnosis: str, n: int) -> str:
    """办一次住院、填病案首页，回入组结果的编码（看停用的组还入不入）。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": f"P21535 县医院{n}", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": f"P21535 病区{n}"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": f"Z{n}"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P21535 患者{n}", "id_card": f"33010619770707{1535 + n:04d}"}).json()["id"]
    adm = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": diagnosis}).json()["id"]
    resp = client.post(f"/api/inpatient/admissions/{adm}/case-summary", headers=admin, json={
        "discharge_diagnosis": diagnosis, "total_cost": 3000})
    assert resp.status_code == 201, resp.text
    return resp.json()["drg_code"]


def test_管理员看到普通组有编辑与停用_停用的组给启用_兜底组只有调权(client, admin):
    stopped = _group(client, admin, "ZP01", active=False)
    groups = {g["code"]: g for g in client.get("/api/drgs/groups", headers=admin).json()}
    out = _run_page(client, admin)
    es31 = groups["ES31"]
    row = _row(out["body"], es31["id"])
    # 修前每行只有「调权」：关键词、主手术、MDC、名称、启停都没有入口
    assert f'<button class="btn secondary" data-drg-edit="{es31["id"]}">编辑</button>' in row, row
    assert f'<button class="btn secondary" data-drg-toggle="{es31["id"]}" data-active="1">停用</button>' in row, row
    assert f'data-drg-weight="{es31["id"]}">调权</button>' in row   # 「调权」照旧
    row = _row(out["body"], stopped["id"])
    assert f'<button class="btn secondary" data-drg-toggle="{stopped["id"]}" data-active="0">启用</button>' in row, row
    assert f'data-drg-edit="{stopped["id"]}"' in row
    fallback = groups["QY"]
    assert fallback["is_fallback"] is True
    row = _row(out["body"], fallback["id"])
    assert f'data-drg-weight="{fallback["id"]}">调权</button>' in row   # 兜底组只有调权
    assert "data-drg-edit" not in row and "data-drg-toggle" not in row, row


def test_非管理员不摆编辑与启停(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21535 医生所在院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    client.post("/api/users", headers=admin, json={"username": "p21535_doc", "password": "passw0rd1",
                                                   "role": "doctor", "org_id": org})
    doctor = login(client, "p21535_doc", "passw0rd1")
    out = _run_page(client, doctor, role="doctor")
    for marker in ("data-drg-weight", "data-drg-edit", "data-drg-toggle"):
        assert marker not in out["body"], marker   # 写接口都是 require_admin，别的角色摆了只会 403


def test_编辑_框里预填原值_只送改了的关键词_预检按新词命中(client, admin):
    g = _group(client, admin, "ZP02", procedure_keywords="P21535缝合术")
    assert _candidates(client, admin, "P21535ZP02病") == ["ZP02"]
    out = _run_page(client, admin, button=f'data-drg-edit="{g["id"]}"', answer={"keywords": "P21535乙病，P21535丙病"})
    assert out["clicked"] is not None, "页面上没有「编辑」按钮"   # 修前
    assert out["modal"] is not None, "点「编辑」没有打开编辑表单"
    fields = out["modal"]["fields"]
    assert [(f["name"], f["type"], f["value"]) for f in fields] == [
        ("name", "text", g["name"]), ("base_weight", "number", 1.1), ("mdc", "text", "MDCZ"),
        ("mdc_name", "text", "P21535 试验大类"), ("keywords", "text", "P21535ZP02病"),
        ("procedure_keywords", "text", "P21535缝合术"), ("require_procedure", "select", "0")]   # 原值、不预先转义（由 spdModal 自己 esc）
    by_name = {f["name"]: f for f in fields}
    assert by_name["name"].get("required") is True
    assert {o["value"] for o in by_name["require_procedure"]["options"]} == {"0", "1"}
    assert "ZP02" in out["modal"]["title"]
    # 只送改了的那一项；成功重画
    assert _writes(out) == [["PATCH", f"/api/drgs/groups/{g['id']}", {"keywords": "P21535乙病，P21535丙病"}]]
    assert out["routed"] == 1 and out["msgs"] == []
    saved = _saved(client, admin, g["id"])
    assert (saved["keywords"], saved["name"], saved["procedure_keywords"], saved["base_weight"]) == (
        "P21535乙病，P21535丙病", g["name"], "P21535缝合术", 1.1)
    assert _candidates(client, admin, "P21535丙病") == ["ZP02"]   # 按新词命中（全角逗号也拆，P1-137）
    assert _candidates(client, admin, "P21535ZP02病") == []       # 旧词不再命中


def test_改名称MDC_勾必须命中主手术_几项照送(client, admin):
    g = _group(client, admin, "ZP03", procedure_keywords="P21535切除术")
    out = _run_page(client, admin, button=f'data-drg-edit="{g["id"]}"', answer={
        "name": "P21535 ZP03 外科组", "mdc": "MDCY", "mdc_name": "P21535 另一大类", "require_procedure": "1"})
    assert _writes(out) == [["PATCH", f"/api/drgs/groups/{g['id']}", {
        "name": "P21535 ZP03 外科组", "mdc": "MDCY", "mdc_name": "P21535 另一大类", "require_procedure": True}]]
    saved = _saved(client, admin, g["id"])
    assert (saved["name"], saved["mdc"], saved["mdc_name"], saved["require_procedure"]) == (
        "P21535 ZP03 外科组", "MDCY", "P21535 另一大类", True)
    assert _candidates(client, admin, "P21535ZP03病") == []   # 外科组：未命中主手术不入
    assert _candidates(client, admin, "P21535ZP03病", "P21535切除术") == ["ZP03"]


def test_编辑框里改基准权重_只送权重(client, admin):
    g = _group(client, admin, "ZP08")
    out = _run_page(client, admin, button=f'data-drg-edit="{g["id"]}"', answer={"base_weight": "0.85"})
    assert _writes(out) == [["PATCH", f"/api/drgs/groups/{g['id']}", {"base_weight": 0.85}]]
    assert out["routed"] == 1
    saved = _saved(client, admin, g["id"])
    assert (saved["base_weight"], saved["keywords"], saved["name"]) == (0.85, g["keywords"], g["name"])


def test_什么都没改_不发请求_消息行提示(client, admin):
    g = _group(client, admin, "ZP04")
    out = _run_page(client, admin, button=f'data-drg-edit="{g["id"]}"', answer={})
    assert out["modal"] is not None
    assert _writes(out) == [] and out["routed"] == 0
    (msg,) = out["msgs"]
    assert msg[0] == "#drg-msg" and msg[2] is False and "没有改动" in msg[1], msg


def test_后端拒收_原话写进消息行_不重画(client, admin):
    g = _group(client, admin, "ZP05")   # 没有主手术关键词
    out = _run_page(client, admin, button=f'data-drg-edit="{g["id"]}"', answer={"require_procedure": "1"})
    assert _writes(out) == [["PATCH", f"/api/drgs/groups/{g['id']}", {"require_procedure": True}]]
    # P2-1019：勾了必须命中主手术却没有主手术关键词，这个组永远入不了——后端 422 的原话
    assert out["msgs"] == [["#drg-msg", "勾了「必须命中主手术」却没有主手术关键词：这个组永远入不了，请填主手术关键词或取消勾选",
                            False]]
    assert out["routed"] == 0
    assert _saved(client, admin, g["id"])["require_procedure"] is False


def test_停用_预检与入组都不再命中_再启用又命中(client, admin):
    g = _group(client, admin, "ZP06")
    assert _candidates(client, admin, "P21535ZP06病") == ["ZP06"]
    out = _run_page(client, admin, button=f'data-drg-toggle="{g["id"]}"')
    assert out["clicked"] is not None, "页面上没有「停用」按钮"   # 修前
    assert _writes(out) == [["PATCH", f"/api/drgs/groups/{g['id']}", {"active": False}]]
    assert out["routed"] == 1 and out["msgs"] == []
    assert _saved(client, admin, g["id"])["active"] is False
    assert _candidates(client, admin, "P21535ZP06病") == []                # 预检不再命中
    assert _grouped_as(client, admin, "P21535ZP06病", 1) == "QY"           # 入组也不再入它（落兜底组）
    out = _run_page(client, admin, button=f'data-drg-toggle="{g["id"]}"')   # 行上这时是「启用」
    assert out["clicked"] == {"drgToggle": str(g["id"]), "active": "0"}
    assert _writes(out) == [["PATCH", f"/api/drgs/groups/{g['id']}", {"active": True}]]
    assert _candidates(client, admin, "P21535ZP06病") == ["ZP06"]
    assert _grouped_as(client, admin, "P21535ZP06病", 2) == "ZP06"


def test_调权照旧_只送基准权重(client, admin):
    g = _group(client, admin, "ZP07")
    out = _run_page(client, admin, button=f'data-drg-weight="{g["id"]}"', answer={"base_weight": "1.35"})
    assert _writes(out) == [["PATCH", f"/api/drgs/groups/{g['id']}", {"base_weight": 1.35}]]
    assert _saved(client, admin, g["id"])["base_weight"] == 1.35
