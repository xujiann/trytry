"""缺药登记清单标出按患者登记的、印出哪天登记的（P2-1661，第四十九批「集中审方与药事监测」扫描 AM3-3 的清单一半）。

修前 `ShortageOut` 没有登记时刻，药事监测页的缺药登记表头只有「ID / 机构 / 药品 / 数量 / 状态 / 操作」：按患者登记的（延伸
处方，登记了不来取药会进黑名单）与按机构补货的分不清，一条登记在途多久了也看不出。

修法：出参**末尾**追加 `created_at`（落库的 naive UTC，与发药记录同一个写法），原有键与次序不动；页面清单加「患者」「登记
时间」两列——患者列印已有的 `patient_id`（`#编号`，没挂患者的为「—」）。**不出患者姓名**：本清单全县可见、收口待 P1-49，
全县可见的清单不先放大患者信息。到货通知与「未取药」等待期另行登记，不在本条。
"""
import json
import os
import shutil
import subprocess

import pytest

from app.database import SessionLocal
from app.models import DrugShortage

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")

#: 修前的出参键（ShortageCreate 的五个、ShortageOut 自己的、P2-793 的 can_handle），新键只许追加在末尾
OLD_KEYS = ["org_id", "patient_id", "drug_code", "drug_name", "quantity", "id", "status", "close_reason", "can_handle"]
PATIENT = "P1661<王五>"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1661 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": PATIENT, "id_card": "330102195001011661"}).json()["id"]
    by_patient = client.post("/api/medication/shortages", headers=admin, json={
        "org_id": org, "patient_id": patient, "drug_code": "P1661A", "drug_name": "P1661 氨氯地平", "quantity": 2})
    assert by_patient.status_code == 201, by_patient.text
    by_org = client.post("/api/medication/shortages", headers=admin, json={
        "org_id": org, "drug_code": "P1661B", "drug_name": "P1661 胰岛素", "quantity": 20})
    assert by_org.status_code == 201, by_org.text
    return {"org": org, "patient": patient, "by_patient": by_patient.json(), "by_org": by_org.json()}


def _row(rows: list[dict], shortage_id: int) -> dict:
    return next(r for r in rows if r["id"] == shortage_id)


def test_登记回执与清单_末尾多出登记时间_原有键与次序不动_不出患者姓名(client, admin, world):
    # 修前没有这个键；P2-1775 又在它后面加了下一步 `next_status_name`（只加在末尾）
    assert list(world["by_patient"]) == OLD_KEYS + ["created_at", "next_status_name"]
    rows = client.get("/api/medication/shortages", headers=admin).json()
    for key in ("by_patient", "by_org"):
        row = _row(rows, world[key]["id"])
        # 全县可见的清单不先放大患者信息（收口待 P1-49）；末尾的 `next_status_name` 是 P2-1775 加的下一步
        assert list(row) == OLD_KEYS + ["created_at", "next_status_name"]
        with SessionLocal() as db:
            stored = db.get(DrugShortage, row["id"])
            assert stored is not None
            assert row["created_at"] == stored.created_at.isoformat()   # 与发药记录同一个写法：落库的 naive UTC


def test_流转与结案回执也带登记时间(client, admin, world):
    advanced = client.post(f"/api/medication/shortages/{world['by_patient']['id']}/advance", headers=admin)
    assert advanced.status_code == 200, advanced.text
    assert advanced.json()["created_at"] == world["by_patient"]["created_at"]
    closed = client.post(f"/api/medication/shortages/{world['by_org']['id']}/close", headers=admin,
                         json={"result": "cancelled", "reason": "药源已解决"})
    assert closed.status_code == 200, closed.text
    assert closed.json()["created_at"] == world["by_org"]["created_at"]


# ---------------------------------------------------------------- 页面：清单两列

pytest_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


#: 页面取数换成桩（写法照 test_case_summary_drg_label 的 `_HARNESS`，放在 shared.js 之前）：`api()` 经管道转给真接口
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


@pytest_node
def test_药事监测页缺药登记清单_印患者与登记时间两列(client, admin, world):
    body = _render(client, admin)
    start = body.index('id="short-form"')
    listing = body[start:body.index("</table>", start)]
    # 修前表头只有 ID / 机构 / 药品 / 数量 / 状态 / 操作
    assert "<th>患者</th>" in listing and "<th>登记时间</th>" in listing, listing
    row = next(r for r in listing.split("<tr>") if r.startswith(f"<td>{world['by_patient']['id']}</td>"))
    assert f"<td>#{world['patient']}</td>" in row, row   # 按患者登记的标出患者编号（不印姓名）
    assert "王五" not in listing
    assert f"<td>{world['by_patient']['created_at'][:16].replace('T', ' ')}</td>" in row, row
    org_row = next(r for r in listing.split("<tr>") if r.startswith(f"<td>{world['by_org']['id']}</td>"))
    assert f"<td>{world['org']}</td><td>—</td>" in org_row, org_row   # 按机构补货的患者列为「—」
