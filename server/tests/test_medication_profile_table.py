"""药事监测页的居民用药画像印成表格，不再整段甩 JSON（P2-1663，第四十九批「集中审方与药事监测」扫描 AM3-5）。

修前 `renderMedication` 的「用药画像查询」把出参整段 `JSON.stringify` 放进 `<pre class="json">`：医生看到的是 `"in_use": true`
这类英文键，`"max_daily_dose": 15.0` 也不带单位——「甩一段 JSON 给人看，是『有端点不等于能用』的另一种形态」，同仓「智能辨证」
早已改成表格。

修法：照「智能辨证」改成表格（药品 / 编码 / 次数 / 最大日剂量 / 是否在用），「同时在用 N 种」放在表上方，一律 `esc()`；
画像出参没有单位字段，最大日剂量不硬造单位。
"""
import json
import os
import shutil
import subprocess
from datetime import timedelta

import pytest

from app import clock
from app.database import SessionLocal
from app.models import Prescription

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def test_用药画像不再整段甩JSON():
    body = _top_level(_read("pages-clinical.js"), "async function renderMedication(")
    assert 'class="json"' not in body and "JSON.stringify(profile" not in body   # 修前 <pre class="json">


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1663 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P1663 患者", "id_card": "330102195001011663"}).json()["id"]
    ids = []
    for code, name, dose in (("P1663-AML", "P1663 氨氯地平<片>", 5), ("P1663-AMX", "P1663 阿莫西林", 1.5)):
        rx = client.post("/api/prescriptions", headers=admin, json={
            "patient_id": patient, "org_id": org, "diagnosis_name": "高血压",
            "items": [{"drug_code": code, "drug_name": name, "daily_dose": dose, "days": 7}]})
        assert rx.status_code == 201 and rx.json()["status"] == "auto_passed", rx.text
        ids.append(rx.json()["id"])
    # 阿莫西林那张是一个月前开的、7 天早吃完了：不在用
    with SessionLocal() as db:
        old = db.get(Prescription, ids[1])
        assert old is not None
        old.created_at = clock.now_naive() - timedelta(days=30)
        db.commit()
    return {"patient": patient}


#: 页面取数换成桩（写法照 test_case_summary_drg_label 的 `_HARNESS`，放在 shared.js 之前）：`api()` 经管道转给真接口；
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
def test_用药画像印成表格_表头与逐行中文(client, admin, world):
    html = _render_profile(client, admin, world["patient"])
    assert 'class="json"' not in html and '"in_use"' not in html, html   # 修前整段 JSON
    assert f"患者 {world['patient']} 同时在用 1 种" in html, html
    assert "<th>药品</th><th>编码</th><th>次数</th><th>最大日剂量</th><th>是否在用</th>" in html, html
    rows = html.split("<tr>")
    in_use = next(r for r in rows if "P1663-AML" in r)
    assert "<td>P1663 氨氯地平&lt;片&gt;</td><td>P1663-AML</td><td>1</td>" in in_use, in_use   # 药名经 esc()
    assert "<td>5</td><td>是</td>" in in_use, in_use
    stopped = next(r for r in rows if "P1663-AMX" in r)
    assert "<td>1.5</td><td>否</td>" in stopped, stopped
