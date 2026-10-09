"""预约页只认编号：核销、取消、代约都凭一串数字，窗口找不出眼前这位是哪一行，代约敲错一位也不提示约给了谁（P2-1700，
第五十批扫描 AN1-3 的预约一半）。

修前（实测 `an1/r1_chain.py`）：`AppointmentOut` 只有 `['slot_id', 'patient_id', 'id', 'status']`——建预约回执、预约清单、
取消与核销回执都是这四个键；预约页的「预约记录」只有「ID / 号源 / 患者 / 状态」四列编号，号源表的机构列印 `org_id`；
代约成功后页面随即重画、一个字不说，患者号敲错一位约给了别人也看不出。同一个「手输编号定位」形状已按 P2-1631（就诊行末尾补
姓名与时刻，页面回显「姓名 · 时间 · 机构」）、P2-1335（住院文书「病区 床号 姓名」）修过。

修法（不加迁移、不动授权，出参只在末尾追加）：
- `AppointmentOut` 末尾加 `patient_name` / `slot_date` / `slot_time` / `resource_name` / `org_name`，四个端点（建预约、
  清单、取消、核销）都从 `appointments._appointments_out` 出，按一批取（姓名一次、号源连机构一次），取不到为空串；
  **建预约回执的 `patient_name` 恒为空串**：建预约不判调用方能不能看这位患者（P1-76 待裁定），回执带姓名就是「敲任意患者号
  约一次号即得姓名」的口子，与签约回执同一取舍（P2-1547）；
- 预约记录加「日期/时段 / 资源 / 放号机构」三列、患者列印「姓名（编号）」；号源表的机构列印机构名（取 `/api/organizations`，
  同消毒供应、医废页）；代约成功后消息行回显「患者编号 · 日期 时段 · 资源」。
- 黑名单清单不动（P1-49 未收口，别扩大暴露面）。
"""
import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import event

from app.database import engine
from conftest import business_today

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

#: 修前 `AppointmentOut` 的键与次序——新键只许接在后面
OLD_KEYS = ["slot_id", "patient_id", "id", "status"]
NEW_KEYS = ["patient_name", "slot_date", "slot_time", "resource_name", "org_name"]


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21700 甲&乙县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patients = []
    for i, name in enumerate(("P21700 张三", "P21700 李<四>", "P21700 王五", "P21700 赵六", "P21700 钱七", "P21700 孙八")):
        resp = client.post("/api/patients", headers=admin, json={
            "name": name, "id_card": f"33010619700101{1700 + i:04d}", "gender": "男"})
        assert resp.status_code == 201, resp.text
        patients.append(resp.json()["id"])
    day = (business_today() + timedelta(days=3)).isoformat()
    slots = []
    for i, slot_time in enumerate(("09:00-10:00", "10:00-11:00")):
        resp = client.post("/api/appointments/slots", headers=admin, json={
            "org_id": org, "resource_type": "outpatient", "resource_name": f"P21700 心内科<专家>{i}",
            "slot_date": day, "slot_time": slot_time, "capacity": 10})
        assert resp.status_code == 201, resp.text
        slots.append(resp.json()["id"])
    return {"org": org, "patients": patients, "slots": slots, "day": day}


def _book(client, admin, slot: int, patient: int) -> dict:
    resp = client.post("/api/appointments", headers=admin, json={"slot_id": slot, "patient_id": patient})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_建预约回执末尾带号源与放号机构_不带姓名_原有键与次序不变(client, admin, world):
    a = _book(client, admin, world["slots"][0], world["patients"][1])
    assert list(a) == OLD_KEYS + NEW_KEYS   # 修前只有前 4 个
    assert (a["slot_id"], a["patient_id"], a["status"]) == (world["slots"][0], world["patients"][1], "booked")
    assert {k: a[k] for k in NEW_KEYS} == {
        "patient_name": "", "slot_date": world["day"], "slot_time": "09:00-10:00",
        "resource_name": "P21700 心内科<专家>0", "org_name": "P21700 甲&乙县医院"}   # 出参原样，转义是页面的事
    # 姓名在清单里给（按可见性收窄）：回执不给，免得敲任意患者号约一次号即得姓名（P1-76 未收口）
    row = next(r for r in client.get("/api/appointments", headers=admin, params={"slot_date": world["day"]}).json()
               if r["id"] == a["id"])
    assert row == {**a, "patient_name": "P21700 李<四>"}


def test_清单行与取消_核销回执同形(client, admin, world):
    keep = _book(client, admin, world["slots"][0], world["patients"][2])
    drop = _book(client, admin, world["slots"][1], world["patients"][2])
    rows = {r["id"]: r for r in client.get("/api/appointments", headers=admin,
                                           params={"slot_date": world["day"]}).json()}
    named = "P21700 王五"
    assert rows[keep["id"]] == {**keep, "patient_name": named} and rows[drop["id"]] == {**drop, "patient_name": named}
    assert all(list(r) == OLD_KEYS + NEW_KEYS for r in rows.values())
    cancelled = client.post(f"/api/appointments/{drop['id']}/cancel", headers=admin)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json() == {**rows[drop["id"]], "status": "cancelled"}
    fulfilled = client.post(f"/api/appointments/{keep['id']}/fulfill", headers=admin)
    assert fulfilled.status_code == 200, fulfilled.text
    assert fulfilled.json() == {**rows[keep["id"]], "status": "fulfilled"}
    assert fulfilled.json()["slot_time"] == "09:00-10:00" and cancelled.json()["slot_time"] == "10:00-11:00"


def _statements(client, admin, params: dict) -> tuple[int, int, list]:
    seen: list[str] = []

    def capture(_conn, _cursor, statement, *_args):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        rows = client.get("/api/appointments", headers=admin, params=params).json()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    return len(rows), len(seen), [s for s in seen if "patients.name" in s]


def test_清单的认人键按一批取_SQL条数不随行数涨(client, admin, world):
    for patient in world["patients"][3:]:
        for slot in world["slots"]:
            _book(client, admin, slot, patient)
    params = {"slot_date": world["day"]}
    _statements(client, admin, {**params, "limit": 1})   # 预热：登录态等缓存
    few, few_sql, few_names = _statements(client, admin, {**params, "limit": 2})
    many, many_sql, many_names = _statements(client, admin, {**params, "limit": 8})
    assert (few, many) == (2, 8)
    assert many_sql == few_sql, (few_sql, many_sql)   # 逐行查库时多 6 行就多十几条
    assert len(few_names) == len(many_names) == 1


# ---------- 页面 ----------


def _top_level(source: str, head: str, end: str = "\n}\n") -> str:
    start = source.index(head)
    return source[start:source.index(end, start) + len(end)]


#: `$()` 按选择器给一个假元素；`api()` 记下每次调用：GET 回 DATA.responses 里的数据，POST 建预约回 DATA.receipt；
#: `route()` 照真页面整页重画的效果把消息行换成新元素——写在重画之前的会被冲掉（P2-1013）；`FormData` 读 DATA.form
_HARNESS = """
const els = {};
const calls = [];
let routed = 0;
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", innerHTML: "", className: "", value: "", dataset: {},
    classList: { add() {}, remove() {} }, addEventListener() {} }); } };
const DATA = JSON.parse(process.argv[1]);
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.FormData = class { get(k) { return (DATA.form || {})[k] ?? null; } };
async function api(path, opts = {}) {
  calls.push([opts.method || "GET", path]);
  if (opts.method === "POST" && path === "/api/appointments") return DATA.receipt;
  if (!(path in DATA.responses)) throw new Error(`夹具里没有 ${path}`);
  return DATA.responses[path];
}
async function route() { routed += 1; delete els["#apt-msg"]; }
async function spdModal() { return DATA.modal ?? null; }
async function postAction() {}
function formJson() { return {}; }
"""


def _run(script: str, data: dict) -> dict:
    out = subprocess.run(["node", "-e", script, json.dumps(data, ensure_ascii=False)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _render(data: dict, after: str = "") -> dict:
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    script = (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8")
              + _top_level(core, "function table(") + _top_level(core, "function panel(")
              + _top_level(core, "function setMsg(") + _top_level(core, "function actionableFirst(")
              + _top_level(core, "function appointmentWho(") + _top_level(core, "async function renderAppointments(")
              + "(async () => { await renderAppointments();\n" + after
              + "  const msg = document.querySelector('#apt-msg');\n"
              "  process.stdout.write(JSON.stringify({ html: els['#page-body'].innerHTML, calls, routed,\n"
              "    msg: [msg.textContent, msg.className] })); })();\n")
    return _run(script, {"responses": RESPONSES, **data})


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")

#: 一行预约（接口真实的形状，见上面的用例）：姓名、资源、机构名里带尖括号与 &，钉住转义
ROW = {"slot_id": 3, "patient_id": 2, "id": 9, "status": "booked", "patient_name": "李<四>", "slot_date": "2026-10-12",
       "slot_time": "09:00-10:00", "resource_name": "心内科<专家>", "org_name": "甲&乙县医院"}
SLOT = {"org_id": 1, "resource_type": "outpatient", "resource_name": "心内科<专家>", "employee_id": None,
        "slot_date": "2026-10-12", "slot_time": "09:00-10:00", "capacity": 5, "id": 3, "booked": 1}
RESPONSES = {
    "/api/appointments/slots": [SLOT, {**SLOT, "id": 4, "org_id": 77}],
    "/api/appointments": [ROW, {**ROW, "id": 8, "status": "fulfilled"}],
    "/api/appointments?status=booked": [ROW],
    "/api/appointments/blacklist": [],
    "/api/organizations": [{"id": 1, "name": "甲&乙县医院"}],
}


@needs_node
def test_页面_号源表印机构名_预约记录带日期时段_资源_机构与姓名_一律转义():
    out = _render({})
    html = out["html"]
    assert ["GET", "/api/organizations"] in out["calls"]
    assert "<td>3</td><td>甲&amp;乙县医院</td>" in html            # 修前印 org_id「1」
    assert "<td>4</td><td>77</td>" in html                          # 机构表里没有的照旧印编号
    assert ("<th>ID</th><th>号源</th><th>日期/时段</th><th>资源</th><th>放号机构</th><th>患者</th><th>状态</th>"
            "<th>操作</th>") in html                                 # 修前只有 ID / 号源 / 患者 / 状态 / 操作
    assert "<td>9</td><td>3</td><td>2026-10-12 09:00-10:00</td>" in html
    assert "<td>心内科&lt;专家&gt;</td><td>甲&amp;乙县医院</td>" in html
    assert "<td>李&lt;四&gt;（2）</td>" in html
    assert "李<四>" not in html and "心内科<专家>" not in html


@needs_node
def test_页面_代约成功先重画再回显约给了谁():
    receipt = {**ROW, "id": 11, "patient_name": ""}
    out = _render({"receipt": receipt, "form": {"slot_id": "3", "patient_id": "2"}},
                  "  await els['#book-form'].onsubmit({ preventDefault() {}, target: {} });\n")
    assert ["POST", "/api/appointments"] in out["calls"]
    assert out["routed"] == 1
    # 写在重画之后（消息行是重画后的新元素）；setMsg 写 textContent，原文不经 HTML 解析，不必也不能再 esc
    assert out["msg"] == ["已预约 #11：患者编号 2 · 2026-10-12 09:00-10:00 · 心内科<专家>——核对号源与患者编号", "msg ok"]


@needs_node
def test_页面_回执缺键时回显写破折号():
    out = _render({"receipt": {"id": 12, "slot_id": 3, "patient_id": 2, "status": "booked"},
                   "form": {"slot_id": "3", "patient_id": "2"}},
                  "  await els['#book-form'].onsubmit({ preventDefault() {}, target: {} });\n")
    assert out["msg"][0] == "已预约 #12：患者编号 2 · — · ———核对号源与患者编号"
