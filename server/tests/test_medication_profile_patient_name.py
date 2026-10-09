"""居民用药画像回显患者姓名：出参末尾带 `patient_name`，页面头一行写「患者 姓名（编号）」（P2-1774，第五十二批扫描 AP4-2 的画像
一半；台账一半见 test_consent_ledger_patient_name.py）。

画像按手输的患者号查（`GET /api/medication/profile/{patient_id}`），原先出参只有编号，页面只印「患者 2 同时在用 N 种」。2026-10-09
开发库实测：医生要查 #1 张三（在用氨氯地平），敲成 2，返回 200、表上是李四的华法林，页面只写「患者 2」——看的是别人的在用药也
认不出来。P2-1631 / P2-1700 / P2-1335 / P2-1727 已定口径：按手输编号定位的都要回显认人键。

修法：`MedicationProfileOut` 末尾只增 `patient_name`（原有键与次序不动）；查看前已判可见性并留痕，回姓名不扩大可见范围（看不到的
照旧 403、不带姓名）。页面头一行写「患者 姓名（编号）」，多重用药与普通两个分支都写，姓名经 esc()。
"""
import json
import os
import shutil
import subprocess

import pytest

from conftest import login

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
#: 修前画像出参的键与次序：只许在末尾加一个 `patient_name`
OLD_KEYS = ["patient_id", "distinct_drugs", "in_use_drugs", "polypharmacy_warning", "drugs"]
NAME = "P1774 <张三>"


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": f"P1774 画像卫生院{i}", "org_type": "township", "level": "township"}).json()["id"] for i in (1, 2)]
    resp = client.post("/api/users", headers=admin, json={
        "username": "p1774_far_doc", "password": "pw123456", "role": "doctor", "org_id": orgs[1]})
    assert resp.status_code in (200, 201), resp.text
    patients = [client.post("/api/patients", headers=admin, json={
        "name": name, "id_card": f"33010619800101177{i}"}).json()["id"] for i, name in enumerate((NAME, "P1774 李四"))]
    # 张三一种在用；李四五种在用（多重用药那个分支）
    plans = {patients[0]: ["P1774-AML"], patients[1]: [f"P1774-W{i}" for i in range(5)]}
    for patient, codes in plans.items():
        for code in codes:
            rx = client.post("/api/prescriptions", headers=admin, json={
                "patient_id": patient, "org_id": orgs[0], "diagnosis_name": "高血压",
                "items": [{"drug_code": code, "drug_name": f"{code} 药", "daily_dose": 1, "days": 30}]})
            assert rx.status_code == 201 and rx.json()["status"] == "auto_passed", rx.text
    return {"patients": patients, "far_doc": login(client, "p1774_far_doc", "pw123456")}


def test_画像出参末尾带患者姓名_原有键与次序不动(client, admin, world):
    for patient, name in zip(world["patients"], (NAME, "P1774 李四")):
        resp = client.get(f"/api/medication/profile/{patient}", headers=admin)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert list(body) == OLD_KEYS + ["patient_name"]   # 修前没有这个键
        assert (body["patient_id"], body["patient_name"]) == (patient, name)


def test_看不到的患者照旧403_不带姓名(client, world):
    resp = client.get(f"/api/medication/profile/{world['patients'][0]}", headers=world["far_doc"])
    assert resp.status_code == 403, resp.text   # 回姓名不扩大可见范围：判可见性在前，一律不动
    assert set(resp.json()) == {"detail"} and "张三" not in resp.text


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


#: 页面取数换成桩（写法照 test_medication_profile_table 的 `_HARNESS`，放在 shared.js 之前）：`api()` 经管道转给真接口；
#: `FormData` 换成按表单对象取值
_HARNESS = r"""
const args = JSON.parse(process.argv[1]);
const els = {};
const mk = () => ({ textContent: "", innerHTML: "", value: "", className: "", dataset: {},
  classList: { add() {}, remove() {} } });
globalThis.document = { addEventListener() {}, cookie: "", querySelector(sel) { return (els[sel] ||= mk()); } };
globalThis.FormData = class { constructor(form) { this.form = form; } get(name) { return this.form[name]; } };
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
async function api(path, opts = {}) {
  process.stdout.write(JSON.stringify({ req: { method: opts.method || "GET", path } }) + "\n");
  const resp = JSON.parse((await lines.next()).value);
  if (resp.status >= 400) throw new Error(errorText(resp.body.detail, `请求失败(${resp.status})`));
  return resp.body;
}
function setMsg() {}
function formJson() { return {}; }
async function postAction() {}
async function spdModal() { return null; }
"""

_RUN = r"""
(async () => {
  await renderMedication();
  await els["#prof-form"].onsubmit({ preventDefault() {}, target: { patient_id: String(args.patient) } });
  process.stdout.write(JSON.stringify({ result: els["#prof-result"].innerHTML }) + "\n");
  rl.close();
})().catch((err) => {
  process.stdout.write(JSON.stringify({ error: String((err && err.stack) || err) }) + "\n");
  rl.close();
});
"""


def _render_profile(client, headers, patient: int) -> str:
    core, page = _read("core.js"), _read("pages-clinical.js")
    script = (_HARNESS + _read("shared.js")
              + "".join(_top_level(core, head) for head in (
                  "function table(", "function panel(", "function actionableFirst(", "function barChart("))
              + _top_level(page, "async function renderMedication(") + _RUN)
    proc = subprocess.Popen(["node", "-e", script, json.dumps({"patient": patient})], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            assert "error" not in message, message["error"]
            resp = client.get(message["req"]["path"], headers=headers)
            proc.stdin.write(json.dumps({"status": resp.status_code, "body": resp.json()}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_画像头一行写患者姓名与编号_两个分支都写_姓名经esc(client, admin, world):
    zhang, li = world["patients"]
    html = _render_profile(client, admin, zhang)
    # 修前「患者 {编号} 同时在用 1 种」，看不出是谁
    assert f'<p class="desc">患者 P1774 &lt;张三&gt;（{zhang}）同时在用 1 种</p>' in html, html
    html = _render_profile(client, admin, li)
    assert f"⚠ 患者 P1774 李四（{li}）多重用药风险：同时在用 5 种" in html, html
