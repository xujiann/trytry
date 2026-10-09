"""退药冲销改成发药记录行上的按钮、确认框写明处方号 / 患者 / 批号×数量；发药记录表印出患者、发药时间、发药人与冲销时间 /
冲销人 / 冲销原因；冲销后再发同一张处方的 409 说清已退药冲销（P2-1539，第四十五批「门诊西药发药与法定医学证明」扫描
AI3-4 的退药那半 + AI3-8）。

修前（scan45 ai3 `r7_typo.py` / `r5_roles_print.py`）：
- 药房页的「退药冲销」是一张只收「发药记录ID」的表单，点了直接提交、没有确认——敲错一位就冲掉别人的发药，药在人家手里，
  账上却回了库，冲销又撤不回；
- 发药记录表只有「ID / 处方ID / 状态 / 明细」，看不出是谁的药、谁发的、谁冲的；冲销人只写进库（`reversed_by`），接口不返回，
  全仓没有一处读；必填的冲销原因填完在页面上看不到；
- 把错发的那条冲销后，真正的主人来取药仍是 409「该处方已发药，不可重复发药」（实际已冲销、须开新处方）。

修后：`DispenseOut` 末尾只增 `reversed_by` 与 `patient_name` / `dispensed_by_name` / `reversed_by_name`（清单按页一次取齐）；
冲销后再发回 409「该处方已退药冲销，不可再发药，须开新处方」，没冲销的重复发药照旧原句；页面上表单换成已发药行上的「退药」
按钮（只给经办 / 药师 / 管理员），点了弹页内框（照同页「召回」），框头写明处方号、患者、批号×数量，原因必填、框自己提交
（P2-607）。发药前回显患者与明细那半另行登记，不在本条。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import login

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
#: 修前 `DispenseOut` 的键与次序：只许在末尾追加
OLD_KEYS = ["id", "prescription_id", "org_id", "status", "dispensed_by", "reverse_reason", "reversed_at", "created_at",
            "items"]
NEW_KEYS = ["reversed_by", "patient_name", "dispensed_by_name", "reversed_by_name"]


@pytest.fixture(scope="module")
def world(client, admin):
    """敲错号的现场：药师甲把甲患者的方号敲成乙患者的，发出去了；账号没填姓名的药师乙把它冲销。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21539 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    users = {}
    for username, full_name in (("p21539_ph1", "P21539 药师甲"), ("p21539_ph2", "")):
        made = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": "pharmacist", "org_id": org, "full_name": full_name})
        assert made.status_code in (200, 201), made.text
        users[username] = made.json()["id"]
    patients = {}
    for key, name, id_card in (("jia", "P21539 甲患者", "330106197001011539"), ("yi", "P21539 乙患者", "330106198001011539")):
        made = client.post("/api/patients", headers=admin, json={"name": name, "id_card": id_card, "gender": "男"})
        assert made.status_code in (200, 201), made.text
        patients[key] = made.json()["id"]
    batch = client.post("/api/pharmacy/batches", headers=admin, json={
        "org_id": org, "drug_code": "P21539-AML", "drug_name": "P21539 氨氯地平片", "batch_no": "AML-1539",
        "expire_date": "2030-12-31", "quantity": 100})
    assert batch.status_code == 201, batch.text
    rx = {}
    for key in ("jia", "yi"):
        made = client.post("/api/prescriptions", headers=admin, json={
            "patient_id": patients[key], "org_id": org, "diagnosis_name": "高血压",
            "items": [{"drug_code": "P21539-AML", "drug_name": "P21539 氨氯地平片", "daily_dose": 1, "days": 28}]})
        assert made.status_code == 201 and made.json()["status"] == "auto_passed", made.text
        rx[key] = made.json()["id"]
    ph1, ph2 = login(client, "p21539_ph1", "passw0rd1"), login(client, "p21539_ph2", "passw0rd1")
    wrong = client.post("/api/dispense", headers=ph1, json={"prescription_id": rx["yi"]})   # 敲错号：发了乙的方
    assert wrong.status_code == 201, wrong.text
    reversed_ = client.post(f"/api/dispense/{wrong.json()['id']}/reverse", headers=ph2,
                            json={"reason": "发错人，当场收回"})
    assert reversed_.status_code == 200, reversed_.text
    kept = client.post("/api/dispense", headers=ph1, json={"prescription_id": rx["jia"]})
    assert kept.status_code == 201, kept.text
    return {"org": org, "users": users, "patients": patients, "rx": rx, "ph1": ph1, "ph2": ph2,
            "wrong": wrong.json(), "reversed": reversed_.json(), "kept": kept.json()}


def test_出参末尾只增冲销人与三个名字_原键次序不动(client, world):
    for body in (world["wrong"], world["reversed"], world["kept"]):   # 发药、冲销两个单条出参
        assert list(body) == OLD_KEYS + NEW_KEYS                       # 修前没有这四个键
    rows = {r["id"]: r for r in client.get(f"/api/dispense?org_id={world['org']}", headers=world["ph1"]).json()}
    assert [list(r) for r in rows.values()] == [OLD_KEYS + NEW_KEYS] * 2
    users = world["users"]
    assert {k: rows[world["wrong"]["id"]][k] for k in NEW_KEYS} == {
        "reversed_by": users["p21539_ph2"], "patient_name": "P21539 乙患者",
        "dispensed_by_name": "P21539 药师甲", "reversed_by_name": "p21539_ph2"}   # 没填姓名的回落账号
    assert {k: rows[world["kept"]["id"]][k] for k in NEW_KEYS} == {
        "reversed_by": None, "patient_name": "P21539 甲患者", "dispensed_by_name": "P21539 药师甲",
        "reversed_by_name": ""}
    assert world["reversed"]["reversed_by_name"] == "p21539_ph2"


def test_冲销后再发409说清须开新处方_没冲销的重复发药照旧原句(client, world):
    again = client.post("/api/dispense", headers=world["ph1"], json={"prescription_id": world["rx"]["yi"]})
    assert again.status_code == 409, again.text
    assert again.json()["detail"] == "该处方已退药冲销，不可再发药，须开新处方"   # 修前「该处方已发药，不可重复发药」
    twice = client.post("/api/dispense", headers=world["ph1"], json={"prescription_id": world["rx"]["jia"]})
    assert twice.status_code == 409, twice.text
    assert twice.json()["detail"] == "该处方已发药，不可重复发药"


# ---------------------------------------------------------------- 页面：药房页原样拿到 node 里跑
PRELUDE = r"""
const ARGS = JSON.parse(process.argv[1]);
const els = {};
const calls = [];
const modals = [];
let routed = 0;
globalThis.localStorage = { getItem: (k) => (k === "medplat_role" ? ARGS.role : null), setItem() {}, removeItem() {} };
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { dataset: {}, innerHTML: "", textContent: "", className: "",
    classList: { add() {}, remove() {} } }); } };
async function api(path, opts = {}) {
  calls.push([opts.method || "GET", path, opts.body ?? null]);
  if (opts.method && opts.method !== "GET") return {};
  if (!(path in ARGS.get)) throw new Error(`没料到的请求：${path}`);
  return JSON.parse(JSON.stringify(ARGS.get[path]));
}
async function spdModal(title, fields, opts = {}) { modals.push({ title, fields, opts }); return ARGS.modalResult; }
function route() { routed += 1; }
function pollTodos() {}
"""


def _function_source(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def _page_gets(client, headers) -> dict:
    """药房页写死的 GET 地址（`api("…")`），以 `headers` 的身份取一遍真接口，留给 node 里的 `api` 回放。
    带 `{ withTotal: true }` 的（已过期仍有余量那一段，P2-1672）照 `api()` 的形状回放成 `{ rows, total }`。"""
    body = _function_source((STATIC / "core.js").read_text(encoding="utf-8"), "async function renderPharmacy(")
    out = {}
    for path, with_total in re.findall(r'\bapi\("([^"]+)"(, \{ withTotal: true \})?\)', body):
        resp = client.get(path, headers=headers)
        assert resp.status_code == 200, (path, resp.text)
        out[path] = {"rows": resp.json(), "total": int(resp.headers["X-Total-Count"])} if with_total else resp.json()
    return out


def _run(role: str, gets: dict, steps: str, modal_result=None):
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    script = (
        PRELUDE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n"
        + _function_source(core, "function table(") + _function_source(core, "function panel(")
        + _function_source(core, "function setMsg(")
        + re.search(r"^function currentRole\(\).*$", core, re.M).group(0) + "\n"
        + _function_source(core, "async function renderPharmacy(")
        + f"\n(async () => {{\nawait renderPharmacy();\n{steps}\n}})().then((r) => process.stdout.write(JSON.stringify(r)),"
        + " (e) => { console.error(e); process.exit(1); });\n"
    )
    payload = json.dumps({"role": role, "get": gets, "modalResult": modal_result}, ensure_ascii=False)
    done = subprocess.run(["node", "-e", script, payload], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _dispense_table(html: str) -> str:
    return html[html.index(">发药记录</h3>"):html.index("批次台账")]


def _row(table: str, dispense_id: int) -> str:
    return re.search(rf"<tr><td>{dispense_id}</td>[\s\S]*?</tr>", table).group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面")
def test_页面_已发药行有退药按钮_已冲销行没有_表上印出发药人冲销人与原因(client, world):
    gets = _page_gets(client, world["ph1"])
    html = _run("pharmacist", gets, "return document.querySelector('#page-body').innerHTML;")
    assert 'id="reverse-form"' not in html and "发药记录ID" not in html   # 修前：只收发药记录号的表单
    table = _dispense_table(html)
    for col in ("患者", "发药时间", "发药人", "冲销时间 / 冲销人 / 冲销原因", "操作"):
        assert f"<th>{col}</th>" in table
    kept, wrong = _row(table, world["kept"]["id"]), _row(table, world["wrong"]["id"])
    assert f'<button class="btn danger" data-reverse="{world["kept"]["id"]}">退药</button>' in kept
    assert "data-reverse" not in wrong                                   # 已冲销的不再摆
    assert "P21539 甲患者" in kept and "P21539 药师甲" in kept
    assert world["kept"]["created_at"][:16].replace("T", " ") in kept   # 发药时间
    assert "P21539 乙患者" in wrong and "P21539 药师甲" in wrong
    assert f'{world["reversed"]["reversed_at"][:16].replace("T", " ")} / p21539_ph2' in wrong   # 冲销时间 / 冲销人
    assert '<span class="desc">发错人，当场收回</span>' in wrong          # 修前必填的原因填完在页面上看不到


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面")
def test_页面_退药弹框写明处方号患者批号数量_不填原因不发_填了才冲销(client, world):
    gets = _page_gets(client, world["ph1"])
    kept = world["kept"]["id"]
    got = _run("pharmacist", gets, f"""
      await document.querySelector("#page-body").onclick({{ target: {{ dataset: {{ reverse: "{kept}" }} }} }});
      const m = modals[0];
      const before = calls.length;
      let blank = null;
      try {{ await m.opts.submit({{ reason: "" }}); }} catch (e) {{ blank = e.message; }}
      const afterBlank = calls.slice(before);
      await m.opts.submit({{ reason: "患者拒收" }});
      return {{ modals: modals.length, title: m.title, intro: m.opts.intro,
               fields: m.fields.map((f) => [f.name, f.type]), blank, afterBlank, posted: calls.slice(before), routed }};
    """, modal_result=True)
    assert got["modals"] == 1                                             # 修前没有确认，点了就提交
    assert got["title"] == f"退药冲销（发药记录 {kept}）"
    assert got["intro"].splitlines() == [
        f"处方 {world['rx']['jia']} · 患者 P21539 甲患者",
        "批号×数量：P21539 氨氯地平片 AML-1539×28",
        "冲销后这张处方不可再发药，确需用药须开新处方",
    ]
    assert got["fields"] == [["reason", "textarea"]]
    assert got["blank"] == "冲销原因必填" and got["afterBlank"] == []        # 不填原因：不发请求
    assert got["posted"] == [["POST", f"/api/dispense/{kept}/reverse", json.dumps({"reason": "患者拒收"},
                                                                                  ensure_ascii=False,
                                                                                  separators=(",", ":"))]]
    assert got["routed"] == 1                                             # 确认后整页重画


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面")
def test_页面_接口不收的角色看得到发药记录_没有退药按钮(client, world):
    """角色口径照 P2-430：退药只收经办 / 药师（管理员放行），医师、管理层只看记录。"""
    gets = _page_gets(client, world["ph1"])
    for role in ("doctor", "director"):
        table = _dispense_table(_run(role, gets, "return document.querySelector('#page-body').innerHTML;"))
        assert "P21539 甲患者" in table
        assert "data-reverse" not in table and "<th>操作</th>" not in table, role
    for role in ("operator", "admin"):
        table = _dispense_table(_run(role, gets, "return document.querySelector('#page-body').innerHTML;"))
        assert f'data-reverse="{world["kept"]["id"]}"' in table, role
