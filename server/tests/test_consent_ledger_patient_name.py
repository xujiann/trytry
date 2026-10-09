"""知情同意台账回显患者姓名，撤回前写明撤的是谁的哪项同意（P2-1774，第五十二批扫描 AP4-2 的台账一半；画像一半见
test_medication_profile_patient_name.py）。

台账按手输的患者号查（`GET /api/consents?patient_id=`），原先出参与台账行里都没有姓名，「撤回」的确认框只写「撤回同意记录 {id}」。
2026-10-09 开发库实测：经办要查张三的同意台账、敲成李四的号，行里认不出是谁；点「撤回」200，李四的跨机构调阅同意被撤，再撤 409——
撤回没有反向端点，撤错了恢复不了。P2-1631 / P2-1700 / P2-1335 / P2-1727 已定口径：按手输编号定位的都要回显认人键。

修法：`ConsentOut` 末尾只增 `patient_name`（原有键与次序不动）。台账与撤回先判可见性并留痕、居民端「我的同意」只取本人与代管成员，
照给姓名，不扩大可见范围；窗口登记（`POST /api/consents`）不判可见性，回执为空串——同预约回执 P2-1700、签约回执 P2-1547，回执带
姓名就成了「敲任意患者号登一条即得姓名」的口子。台账加「患者」列印「姓名（编号）」（经 esc()），撤回的确认框写「撤回 姓名 的
「场景」同意」（原生确认框是纯文本，不转义）。
"""
import json
import os
import shutil
import subprocess

import pytest

from app.database import SessionLocal
from app.models import SmsCode

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
#: 修前同意记录出参的键与次序：只许在末尾加一个 `patient_name`
OLD_KEYS = ["id", "patient_id", "scene", "scene_name", "text_version", "method", "method_name", "operator_user_id",
            "resident_account_id", "evidence", "guardian_name", "guardian_id_card", "guardian_relation",
            "guardian_relation_name", "revoked_at", "created_at"]
NAME = "P1774 <李四> & 子"
PHONE = "13800017741"


@pytest.fixture(scope="module")
def world(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": NAME, "id_card": "330106198501011774", "phone": PHONE}).json()["id"]
    resp = client.post("/api/consents", headers=admin, json={
        "patient_id": patient, "scene": "cross_org_access", "evidence": "签字影像#P1774"})
    assert resp.status_code == 201, resp.text
    return {"patient": patient, "receipt": resp.json()}


def test_窗口登记回执末尾有姓名键_值为空串(world):
    receipt = world["receipt"]
    assert list(receipt) == OLD_KEYS + ["patient_name"]
    assert receipt["patient_name"] == ""   # 登记不判可见性：回执不带姓名，免得成了按患者号查姓名的口子


def test_台账行末尾带患者姓名_原有键与次序不动(client, admin, world):
    rows = client.get("/api/consents", headers=admin, params={"patient_id": world["patient"]}).json()
    assert [list(r) for r in rows] == [OLD_KEYS + ["patient_name"]]   # 修前没有这个键
    assert (rows[0]["patient_id"], rows[0]["patient_name"]) == (world["patient"], NAME)


def test_撤回回执带患者姓名(client, admin, world):
    resp = client.post("/api/consents", headers=admin, json={
        "patient_id": world["patient"], "scene": "followup", "evidence": "签字影像#P1774-2"})
    assert resp.status_code == 201, resp.text
    revoked = client.post(f"/api/consents/{resp.json()['id']}/revoke", headers=admin)
    assert revoked.status_code == 200, revoked.text
    assert list(revoked.json()) == OLD_KEYS + ["patient_name"]
    assert revoked.json()["patient_name"] == NAME


def _portal_login(client, phone: str) -> dict:
    """居民手机号登录（写法照 test_consents 的 `portal_login`）：按手机号自动实名绑定到自己的档案。"""
    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()
    code = client.post("/api/portal/auth/sms/code", json={"phone": phone, "purpose": "login"}).json()["debug_code"]
    body = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code}).json()
    return {"Authorization": f"Bearer {body['access_token']}"}


def test_居民端我的同意同形_带本人姓名(client, world):
    me = _portal_login(client, PHONE)
    signed = client.post("/api/portal/me/consents", headers=me, json={"scene": "archive"})
    assert signed.status_code == 201, signed.text
    assert list(signed.json()) == OLD_KEYS + ["patient_name"] and signed.json()["patient_name"] == NAME
    mine = client.get("/api/portal/me/consents", headers=me).json()
    assert mine and {r["patient_name"] for r in mine} == {NAME}


# ---------------------------------------------------------------- 页面：台账「患者」列与撤回确认框


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


#: 页面取数换成桩（写法照 test_medication_profile_table 的 `_HARNESS`，放在 shared.js 之前）：`api()` 经管道转给真接口；
#: `confirm` 记下文案、答「取消」（不真撤）；经办身份（摆「撤回」、不取待审清单）
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
const confirms = [];
globalThis.confirm = (text) => { confirms.push(text); return false; };
function currentRole() { return "operator"; }
function setMsg() {}
async function openPrintPage() {}
async function spdModal() { return null; }
"""

_RUN = r"""
(async () => {
  await renderConsents();
  await els["#ct-search"].onsubmit({ preventDefault() {}, target: { patient_id: String(args.patient) } });
  const ledger = els["#ct-table"].innerHTML;
  await els["#ct-table"].onclick({ target: { dataset: { revokeConsent: String(args.consent) } } });
  process.stdout.write(JSON.stringify({ result: { ledger, confirms } }) + "\n");
  rl.close();
})().catch((err) => {
  process.stdout.write(JSON.stringify({ error: String((err && err.stack) || err) }) + "\n");
  rl.close();
});
"""


def _render_consents(client, headers, patient: int, consent: int) -> dict:
    core, page = _read("core.js"), _read("pages-clinical.js")
    script = (_HARNESS + _read("shared.js")
              + "".join(_top_level(core, head) for head in ("function table(", "function panel("))
              + page[page.index("const TEXT_STATUS = "):page.index("\n", page.index("const TEXT_STATUS = "))] + "\n"
              + _top_level(page, "async function renderConsents(") + _RUN)
    proc = subprocess.Popen(["node", "-e", script, json.dumps({"patient": patient, "consent": consent})],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            assert "error" not in message, message["error"]
            assert message["req"]["method"] == "GET", message   # 确认框答了「取消」：不该有撤回请求
            resp = client.get(message["req"]["path"], headers=headers)
            proc.stdin.write(json.dumps({"status": resp.status_code, "body": resp.json()}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_台账印患者姓名_撤回确认框写明撤谁的哪项同意(client, admin, world):
    consent = world["receipt"]["id"]
    out = _render_consents(client, admin, world["patient"], consent)
    ledger = out["ledger"]
    assert "<th>患者</th>" in ledger, ledger   # 修前表头没有「患者」
    row = next(r for r in ledger.split("<tr>") if f'data-revoke-consent="{consent}"' in r)
    assert f"<td>P1774 &lt;李四&gt; &amp; 子（{world['patient']}）</td>" in row, row   # 经 esc()
    # 修前「撤回同意记录 {id}？……」：看不出撤的是谁；原生确认框是纯文本，姓名照原样
    assert out["confirms"] == [
        f"撤回 {NAME} 的「跨机构调阅」同意（记录 {consent}）？记录会保留并标记撤回时刻，但无法再恢复为有效。"]
