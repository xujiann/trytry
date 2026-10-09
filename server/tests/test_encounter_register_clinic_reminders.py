"""「诊间」医防提醒只能在「公卫协同」页手输患者 ID 查，门诊接诊登记、360 视图、医生移动端都不显示（P2-1434，第四十二批
扫描 AF2-8）。

提醒接口的说明写着「接诊时汇聚该患者的公卫待办与风险提示」，用户手册也写就诊记录「经"公卫协同"诊间提醒联动」；前端却只有
公卫协同页调它（`grep -rn reminders app/static` 只有那一处）——医生接诊时看不到这位患者随访超期、疫苗禁忌、处置中的公卫
事件，要换到别的页面、手输数字患者 ID 才看得到。

修法（只改页面，接口与权限不动）：门诊接诊登记成功后，先重画、写「登记成功」，再取这位患者的诊间提醒列在登记面板下；
没有提醒写「暂无公卫提醒」，取不到写原因、不影响「登记成功」。先登记、后取：本机构刚登记了这位患者的就诊，即有调阅依据
（`visibility.patient_basis` 的「本机构就诊过」），提醒接口照旧校验可见性并留痕。用户手册第三章「接诊」补一句。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import login

from app.database import SessionLocal
from app.models import AccessLog

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21434 甲镇卫生院", "org_type": "township", "level": "township"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21434_doc", "password": "passw0rd1", "role": "doctor", "org_id": org})
    assert created.status_code in (200, 201), created.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21434 患者", "id_card": "330106197001011434", "gender": "女"}).json()["id"]
    contra = client.post("/api/vaccination/contraindications", headers=admin, json={
        "patient_id": patient, "vaccine_code": "HPV9", "reason": "青霉素<过敏>", "contra_type": "permanent"})
    assert contra.status_code == 201, contra.text
    return {"org": org, "patient": patient, "doctor": login(client, "p21434_doc", "passw0rd1")}


def test_本机构刚登记就诊_即可取这位患者的提醒_取提醒照旧留痕(client, world):
    """页面「先登记、后取」的依据：登记之前与本机构没有关系，取提醒 403；登记了就诊就有「本机构就诊过」的依据，200 且留痕。"""
    doctor, patient = world["doctor"], world["patient"]
    before = client.get(f"/api/publichealth/reminders/{patient}", headers=doctor)
    assert before.status_code == 403, before.text   # 接口的可见性校验照旧，没有放宽
    enc = client.post("/api/encounters", headers=doctor, json={
        "patient_id": patient, "org_id": world["org"], "diagnosis_name": "上呼吸道感染"})
    assert enc.status_code == 201, enc.text
    after = client.get(f"/api/publichealth/reminders/{patient}", headers=doctor)
    assert after.status_code == 200, after.text
    assert {"type": "vaccine_contraindication", "detail": "疫苗 HPV9 禁忌：青霉素<过敏>"} in after.json()["reminders"]
    with SessionLocal() as db:
        logs = db.query(AccessLog).filter(AccessLog.username == "p21434_doc", AccessLog.patient_id == patient).all()
    assert [(log.resource, log.basis) for log in logs] == [("publichealth", "encounter")]   # 被拒的那次不留痕


# ---------- 页面：登记成功就地取提醒 ----------


def _top_level(source: str, head: str, end: str = "\n}\n") -> str:
    start = source.index(head)
    return source[start:source.index(end, start) + len(end)]


#: `$()` 按选择器给一个假元素；`api()` 记下每次调用：POST 登记回给定回执（或抛给定的错），取提醒回给定数据（或抛错），
#: 其余 GET 回空清单（带 `withTotal` 的就诊表回空的 `{ rows, total }`，P2-1631 起接诊页读总数）；`route()` 照真页面整页
#: 重画的效果把消息行与提醒区换成新元素——写在重画之前的会被冲掉（P2-1013）
_HARNESS = """
let els = {};
const calls = [];
let routed = 0;
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", innerHTML: "", className: "", listeners: {},
    classList: { add() {}, remove() {} }, addEventListener(type, fn) { this.listeners[type] = fn; } }); } };
const DATA = JSON.parse(process.argv[1]);
async function api(path, opts = {}) {
  calls.push([opts.method || "GET", path, opts.body ? JSON.parse(opts.body) : null]);
  if (opts.method === "POST") {
    if (DATA.postError) throw new Error(DATA.postError);
    return DATA.encounter;
  }
  if (path.startsWith("/api/publichealth/reminders/")) {
    if (DATA.remindersError) throw new Error(DATA.remindersError);
    return DATA.reminders;
  }
  return opts.withTotal ? { rows: [], total: 0 } : [];
}
async function route() { routed += 1; delete els["#enc-msg"]; delete els["#enc-reminders"]; }
function formJson() { return DATA.body; }
function postAction() { throw new Error("门诊接诊登记不该再走 postAction"); }
"""


def _run_page(data: dict) -> dict:
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    page = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    script = (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8")
              + _top_level(core, "function table(") + _top_level(core, "function panel(")
              + _top_level(core, "function setMsg(") + _top_level(page, "const ENC_FILTER", "\n")
              + _top_level(page, "async function renderArchive(")
              + "(async () => { await renderArchive();\n"
              "  await els['#enc-form'].onsubmit({ preventDefault() {}, target: {} });\n"
              "  const msg = document.querySelector('#enc-msg');\n"
              "  process.stdout.write(JSON.stringify({ msg: [msg.textContent, msg.className], routed,\n"
              "    reminders: document.querySelector('#enc-reminders').innerHTML,\n"
              "    calls: calls.filter((c) => !c[1].startsWith('/api/encounters?')) })); })();\n")
    out = subprocess.run(["node", "-e", script, json.dumps(data, ensure_ascii=False)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")


@needs_node
def test_页面_登记成功先重画再写回执_就地列出这位患者的提醒_一律转义(client, world):
    doctor, patient = world["doctor"], world["patient"]
    body = {"patient_id": patient, "org_id": world["org"], "diagnosis_name": "高血压复诊"}
    enc = client.post("/api/encounters", headers=doctor, json=body)
    assert enc.status_code == 201, enc.text
    reminders = client.get(f"/api/publichealth/reminders/{patient}", headers=doctor).json()   # 真接口的返回
    out = _run_page({"body": body, "encounter": enc.json(), "reminders": reminders})
    # 修前走 postAction：登记完只重画，一个提醒都不取
    assert out["calls"] == [["POST", "/api/encounters", body], ["GET", f"/api/publichealth/reminders/{patient}", None]]
    assert out["routed"] == 1
    assert out["msg"] == [f"登记成功（就诊ID {enc.json()['id']}）", "msg ok"]
    assert f"患者 {patient} 的诊间公卫提醒：" in out["reminders"]
    assert "<li>疫苗 HPV9 禁忌：青霉素&lt;过敏&gt;</li>" in out["reminders"]


@needs_node
def test_页面_没有提醒写暂无公卫提醒():
    out = _run_page({"body": {"patient_id": 7, "org_id": 3}, "encounter": {"id": 41, "patient_id": 7},
                     "reminders": {"patient_id": 7, "reminders": []}})
    assert out["msg"] == ["登记成功（就诊ID 41）", "msg ok"]
    assert out["reminders"] == '<p class="msg ok">暂无公卫提醒</p>'


@needs_node
def test_页面_提醒取不到写原因_登记成功照写():
    out = _run_page({"body": {"patient_id": 7, "org_id": 3}, "encounter": {"id": 42, "patient_id": 7},
                     "remindersError": "无权调阅该患者档案：<本机构>无关系"})
    assert out["msg"] == ["登记成功（就诊ID 42）", "msg ok"]
    assert out["reminders"] == '<p class="msg err">公卫提醒取不到：无权调阅该患者档案：&lt;本机构&gt;无关系</p>'


@needs_node
def test_页面_登记失败写原因_不重画也不取提醒():
    out = _run_page({"body": {"patient_id": 7, "org_id": 3}, "postError": "无权以该机构名义写入数据"})
    assert out["msg"] == ["无权以该机构名义写入数据", "msg err"]
    assert out["routed"] == 0
    assert [c[0] for c in out["calls"]] == ["POST"]
    assert out["reminders"] == ""
