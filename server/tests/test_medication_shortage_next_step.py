"""缺药登记的推进按钮写明下一步，出参末尾带 `next_status_name`；用户手册把缺药流转与用药监测说对（P2-1775，第五十二批扫描 AP4-6）。

修前药事监测页缺药登记的按钮一律写「流转」：采购中的登记再点一下就记成「已配送」——随即可以判「未取药」、退出在途与供应风险，
而推进没有反向端点（已配送再推 409「请结案」）。用户手册写「推进"采购中→已送达"」：缺药流转里没有「已送达」，也没写结案这一步；
又写「用药监测（重复用药预警）」，画像只报多重用药（同时在用的品种数达到阈值），跨处方的同药重复不报（P2-805 待裁定）。
P2-1407 已定「按钮写明下一步，点下去记成的就是按钮上写的那一步」（代煎单）。

修法：`ShortageOut` 末尾只增 `next_status_name`（下一步状态的中文，与推进同一张 `_SHORTAGE_FLOW`；已配送与结案三态为空串），
页面按钮写「标为采购中 / 标为已配送」，不另抄流转表；手册改为「已登记→采购中→已配送，药到后结案（已取药 / 未取药 / 已取消）」
「用药画像只有多重用药预警」。
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.routers.medication import _SHORTAGE_CLOSED, _SHORTAGE_FLOW, SHORTAGE_STATUS_NAMES

ROOT = Path(__file__).resolve().parents[2]
STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
#: 修前缺药登记出参的键与次序（P2-1661 的 created_at 在内）：只许在末尾加一个 `next_status_name`
OLD_KEYS = ["org_id", "patient_id", "drug_code", "drug_name", "quantity", "id", "status", "close_reason", "can_handle",
            "created_at"]


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P1775 卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _register(client, admin, org, code="P1775-INS"):
    resp = client.post("/api/medication/shortages", headers=admin, json={
        "org_id": org, "drug_code": code, "drug_name": "甘精胰岛素", "quantity": 2})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _advance(client, admin, sid):
    resp = client.post(f"/api/medication/shortages/{sid}/advance", headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_出参末尾只增下一步_已配送与结案为空串(client, admin, org):
    created = _register(client, admin, org)
    assert list(created) == OLD_KEYS + ["next_status_name"]   # 修前没有这个键
    assert (created["status"], created["next_status_name"]) == ("registered", "采购中")
    purchasing = _advance(client, admin, created["id"])
    assert (purchasing["status"], purchasing["next_status_name"]) == ("purchasing", "已配送")
    rows = {r["id"]: r for r in client.get("/api/medication/shortages", headers=admin).json()}
    assert list(rows[created["id"]]) == OLD_KEYS + ["next_status_name"]
    assert rows[created["id"]]["next_status_name"] == "已配送"   # 清单行同一个口径
    delivered = _advance(client, admin, created["id"])
    assert (delivered["status"], delivered["next_status_name"]) == ("delivered", "")   # 已配送：该结案了，不再推进
    closed = client.post(f"/api/medication/shortages/{created['id']}/close", headers=admin,
                         json={"result": "collected", "reason": ""})
    assert closed.status_code == 200, closed.text
    assert (closed.json()["status"], closed.json()["next_status_name"]) == ("collected", "")
    cancelled = client.post(f"/api/medication/shortages/{_register(client, admin, org)['id']}/close", headers=admin,
                            json={"result": "cancelled", "reason": "登错"})
    assert (cancelled.json()["status"], cancelled.json()["next_status_name"]) == ("cancelled", "")


def test_按钮写的下一步就是点下去真走的那一步(client, admin, org):
    shortage = _register(client, admin, org, "P1775-AML")
    while shortage["next_status_name"]:
        promised = shortage["next_status_name"]
        shortage = _advance(client, admin, shortage["id"])
        assert SHORTAGE_STATUS_NAMES[shortage["status"]] == promised
    assert shortage["status"] == "delivered"
    resp = client.post(f"/api/medication/shortages/{shortage['id']}/advance", headers=admin)
    assert resp.status_code == 409 and resp.json()["detail"] == "已配送的登记不能再推进，请结案", resp.text


# ---------------------------------------------------------------- 页面：按钮文字随状态


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


#: 页面取数换成桩（写法照 test_medication_shortage_list_patient_time 的 `_HARNESS`，放在 shared.js 之前）：`api()` 经管道转给真接口
_HARNESS = r"""
const els = {};
const mk = () => ({ textContent: "", innerHTML: "", value: "", className: "", dataset: {},
  classList: { add() {}, remove() {} } });
globalThis.document = { addEventListener() {}, cookie: "", querySelector(sel) { return (els[sel] ||= mk()); } };
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
  process.stdout.write(JSON.stringify({ result: els["#page-body"].innerHTML }) + "\n");
  rl.close();
})().catch((err) => {
  process.stdout.write(JSON.stringify({ error: String((err && err.stack) || err) }) + "\n");
  rl.close();
});
"""


def _render(client, headers) -> str:
    core, page = _read("core.js"), _read("pages-clinical.js")
    script = (_HARNESS + _read("shared.js")
              + "".join(_top_level(core, head) for head in (
                  "function table(", "function panel(", "function actionableFirst(", "function barChart("))
              + _top_level(page, "async function renderMedication(") + _RUN)
    proc = subprocess.Popen(["node", "-e", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
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
def test_页面按钮写标为采购中_标为已配送_已配送不摆推进(client, admin, org):
    registered = _register(client, admin, org, "P1775-R")["id"]
    purchasing = _register(client, admin, org, "P1775-P")["id"]
    _advance(client, admin, purchasing)
    delivered = _register(client, admin, org, "P1775-D")["id"]
    _advance(client, admin, delivered)
    _advance(client, admin, delivered)
    body = _render(client, admin)
    start = body.index('id="short-form"')
    listing = body[start:body.index("</table>", start)]
    buttons = dict(re.findall(r'data-adv="(\d+)">([^<]*)</button>', listing))
    assert buttons[str(registered)] == "标为采购中"   # 修前一律「流转」
    assert buttons[str(purchasing)] == "标为已配送"   # 修前「流转」：点下去记成已配送，看不出来
    assert str(delivered) not in buttons and ">流转</button>" not in listing


# ---------------------------------------------------------------- 手册：缺药流转与用药监测的两句


def _shortage_step() -> str:
    manual = (ROOT / "docs" / "用户手册.md").read_text(encoding="utf-8")
    chapter = manual[manual.index("## 第四章 药师（pharmacist）"):]
    chapter = chapter[:chapter.index("\n## ")]
    start = chapter.index("3. **短缺处置**")
    return chapter[start:chapter.index("\n4. **", start)]


def test_手册写的缺药流转与后端状态机一致_写明结案():
    step = _shortage_step()
    assert "已送达" not in step, step   # 修前「推进"采购中→已送达"」：缺药流转里没有这个状态
    flow = ["registered", *_SHORTAGE_FLOW.values()]
    assert "→".join(SHORTAGE_STATUS_NAMES[s] for s in flow) in step, step   # 已登记→采购中→已配送
    assert all(f"标为{SHORTAGE_STATUS_NAMES[s]}" in step for s in _SHORTAGE_FLOW.values()), step   # 按钮上的字
    assert "结案" in step and all(SHORTAGE_STATUS_NAMES[s] in step for s in _SHORTAGE_CLOSED), step   # 修前没写结案


def test_手册写的用药监测只有多重用药预警():
    step = _shortage_step()
    assert "重复用药预警" not in step, step   # 修前「用药监测（重复用药预警）」：画像只报多重用药
    assert "多重用药预警" in step, step
