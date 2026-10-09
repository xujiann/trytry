"""居民端「我的预约（N）」数的是本页条数；超过 100 条时，最早约下的近期号被截掉，看不到也取消不了（P2-1702，第五十批扫描
AN1-6）。

修前（实测 `an1/r2_lists.py`）：`m.js` 的 `renderAppointments` 不带参数取 `/api/portal/me/appointments`——接口缺省 100 条、
按编号倒序。本人一共 106 条预约，只回 100 条（X-Total-Count 106），标题印「我的预约（100）」；最早约下的那几个号（也就是最近
要去的）不在页上，手机上取消不了，页面也不说截断。同页「可约号源」拿满一页早有截断提示；同一形状的判例是 P2-1550（标题取
总数）、P2-1547 / P2-1631（列不全时写「已列 N / 共 M」）、P2-1674（未结束的单独取全、排最前）。

修法：接口加可选 `status`（取值照预约状态列注释，`pattern` 限定、写错 422），叠在「本人 + 代管成员」之后，缺省不动（同管理端
P2-1300）；页面「已预约」的按 `status=booked` 续页取全（`fetchAllPages`）、按号源日期时段排在最前，其后接缺省清单、按 id
去重（照 P2-1674），经 `withTotal` 读 X-Total-Count，列不全时标题写「已列 N / 共 M」并提示。

页面那几条把 `m.js` 的 `api()`、取数与渲染函数原文放进 node 跑，`fetch` 经管道转给真接口（以居民本人的身份），响应头照
真接口给——`withTotal` 读到的是真的 X-Total-Count。
"""
import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert, select

from app.database import SessionLocal
from app.models import Appointment, AppointmentSlot
from conftest import business_today

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
P = "/api/portal/me/appointments"
#: 本人的预约条数：比清单缺省一页（100）多 6 条
N = 106
#: 编号最小（约得最早、号源日期最近）的这几条是「已预约」——缺省那一页（编号倒序前 100 条）里没有它们
EARLY = 5
#: `PortalAppointmentOut` 的键序，修法不动
KEYS = ["id", "patient_id", "patient_name", "org_name", "resource_name", "slot_date", "slot_time", "status"]


def _status(i: int) -> str:
    """第 i 条（从 0 数，编号与号源日期同序）的状态：第 0 条已取消，1..5 已预约，其余已预约 / 已取消相间。"""
    if i == 0:
        return "cancelled"
    if i <= EARLY:
        return "booked"
    return ("booked", "cancelled")[i % 2]


def _resident(client, admin, n: int, phone: str, count: int, org: int) -> dict:
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P21702 居民{n}", "id_card": f"33010219700101170{n}", "gender": "男", "birth_date": "1970-01-01",
        "phone": phone})
    assert patient.status_code == 201, patient.text
    pid = patient.json()["id"]
    start = business_today() + timedelta(days=1)
    with SessionLocal() as db:   # 逐条走接口太慢，直接落库：一个号源一条预约，号源日期随编号递增
        db.execute(insert(AppointmentSlot), [{
            "org_id": org, "resource_type": "outpatient", "resource_name": f"P21702 全科{n}-{i:03d}",
            "slot_date": (start + timedelta(days=i // 3)).isoformat(), "slot_time": f"{8 + i % 3:02d}:00-{9 + i % 3:02d}:00",
            "capacity": 5, "booked": 1} for i in range(count)])
        slot_ids = db.scalars(select(AppointmentSlot.id).where(
            AppointmentSlot.resource_name.like(f"P21702 全科{n}-%")).order_by(AppointmentSlot.id)).all()
        db.execute(insert(Appointment), [
            {"slot_id": sid, "patient_id": pid, "status": _status(i)} for i, sid in enumerate(slot_ids)])
        db.commit()
        ids = db.scalars(select(Appointment.id).where(Appointment.patient_id == pid).order_by(Appointment.id)).all()
    code = client.post("/api/portal/auth/sms/code", json={"phone": phone}).json()["debug_code"]
    resident = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code})
    assert resident.status_code == 200 and resident.json()["bound"], resident.text
    return {"patient": pid, "ids": ids, "resident": {"Authorization": f"Bearer {resident.json()['access_token']}"}}


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P21702 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]


@pytest.fixture(scope="module")
def world(client, admin, org):
    """扫描的现场：本人 106 条预约，最早约下的 5 条已预约、号源日期最近。"""
    return _resident(client, admin, 0, "13900017020", N, org)


@pytest.fixture(scope="module")
def few(client, admin, org):
    """另一位：只有 3 条预约（一页列得下）。"""
    return _resident(client, admin, 1, "13900017021", 3, org)


def test_接口缺省调用照旧_最早约的已预约不在缺省那一页(client, world):
    resp = client.get(P, headers=world["resident"])
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert (len(rows), resp.headers["X-Total-Count"]) == (100, str(N))
    assert [r["id"] for r in rows] == world["ids"][::-1][:100]   # 编号倒序，缺省与字节不动
    assert list(rows[0]) == KEYS
    assert not set(world["ids"][1:EARLY + 1]) & {r["id"] for r in rows}   # 最近要去的那 5 个号不在这一页


def test_按状态取_只回本人与代管成员的该状态(client, world, few):
    resp = client.get(P, headers=world["resident"], params={"status": "booked", "limit": 500})
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    booked = [i for k, i in enumerate(world["ids"]) if _status(k) == "booked"]
    assert sorted(r["id"] for r in rows) == booked   # 修前 ?status= 被忽略，照回最新 100 条
    assert resp.headers["X-Total-Count"] == str(len(booked))
    assert {r["status"] for r in rows} == {"booked"} and list(rows[0]) == KEYS
    assert not set(few["ids"]) & {r["id"] for r in rows}   # 别人的已预约不混进来
    done = client.get(P, headers=world["resident"], params={"status": "fulfilled"})
    assert done.json() == [] and done.headers["X-Total-Count"] == "0"


def test_状态写错_422(client, world):
    for bad in ("noshow", "BOOKED", "booked "):
        resp = client.get(P, headers=world["resident"], params={"status": bad})
        assert resp.status_code == 422, (bad, resp.text)   # 修前 200，照回最新一页


# ------------------------------------------------------------------ 页面

PRELUDE = r"""
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const requests = [];
const els = {};
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { value: "", innerHTML: "", addEventListener() {},
    querySelectorAll() { return []; }, insertAdjacentHTML() {} }); } };
/* 浏览器的 fetch：经管道转给真接口，响应头只垫 X-Total-Count（大小写不敏感，照 Headers.get） */
globalThis.fetch = async (path, init = {}) => {
  requests.push(path);
  process.stdout.write(JSON.stringify({ path }) + "\n");
  const reply = JSON.parse((await lines.next()).value);
  return { status: reply.status, ok: reply.status < 400, json: async () => reply.body,
    headers: { get: (name) => (name.toLowerCase() === "x-total-count" ? reply.total : null) } };
};
/* 登录态请求：Cookie 会话由管道那头补上居民本人的令牌 */
async function authApi(path, options = {}) { return api(path, options); }
async function loadService() {}
let viewingPatientId = null;
const box = { innerHTML: "", querySelectorAll: () => [] };
"""


def _top(source: str, head: str) -> str:
    """顶层声明原文：函数取到它之后第一个顶格的 `}`，跨行的对象常量取到顶格的 `};`，其余常量取这一行。"""
    start = source.index(head)
    if head.startswith(("function ", "async function ")):
        return source[start:source.index("\n}\n", start) + 3]
    line_end = source.index("\n", start)
    if source[start:line_end].endswith("{"):
        return source[start:source.index("\n};\n", start) + 4]
    return source[start:line_end + 1]


def run_page(client, headers: dict, steps: str):
    """在 node 里加载 shared.js 与 `m.js` 的取数 / 渲染函数，跑 `steps`（async 函数体，return 一个可 JSON 化的值）。"""
    source = (STATIC / "m" / "m.js").read_text(encoding="utf-8")
    script = (
        PRELUDE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n"
        + "".join(_top(source, head) for head in (
            "async function api(", "function kv(", "const APPT_STATUS = ", "const SLOT_PAGE = ",
            "async function fetchMyAppointments(", "async function renderAppointments("))
        + f"\n(async () => {{\n{steps}\n}})().then("
        + "(r) => { process.stdout.write(JSON.stringify({ result: r }) + '\\n'); rl.close(); },"
        + " (e) => { console.error(e); process.exit(1); });\n"
    )
    proc = subprocess.Popen(["node", "-e", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            resp = client.get(message["path"], headers=headers)
            reply = {"status": resp.status_code, "body": resp.json(), "total": resp.headers.get("x-total-count")}
            proc.stdin.write(json.dumps(reply, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=60)


_STEPS = """
  const { rows, total } = await fetchMyAppointments();
  await renderAppointments(box);
  return { rows: rows.map((a) => [a.id, a.status, a.slot_date, a.slot_time]), total, requests, html: box.innerHTML };
"""


def test_页面取数_近期待就诊的排最前_写明已列与总数(client, world):
    out = run_page(client, world["resident"], _STEPS)
    rows, total = out["rows"], out["total"]
    booked = [r for r in rows if r[1] == "booked"]
    assert [r[0] for r in rows[:EARLY]] == world["ids"][1:EARLY + 1], rows[:EARLY + 1]   # 修前这 5 条根本不在页上
    assert rows[:len(booked)] == booked                                   # 已预约的全在最前
    assert [(r[2], r[3], r[0]) for r in booked] == sorted((r[2], r[3], r[0]) for r in booked)   # 按号源日期时段排
    assert total == N
    # 已预约的全部（1..5 与第 6 条以后的一半）+ 缺省那一页里其余的（已取消），按 id 去重；第 0 条已取消、编号最小，不在
    assert len({r[0] for r in rows}) == len(rows) == N - 1
    assert world["ids"][0] not in [r[0] for r in rows]
    assert P in out["requests"]                                            # 缺省清单照旧不带参数取
    assert f"{P}?status=booked&limit=500&offset=0" in out["requests"]      # 已预约的续页取全
    assert f'<div class="sec-title">我的预约（已列 {N - 1} / 共 {N}）</div>' in out["html"]   # 修前「我的预约（100）」
    assert "待就诊的全部列出、排在最前" in out["html"]
    assert f'data-id="{world["ids"][1]}"' in out["html"]                    # 最近要去的那个号有「取消预约」


def test_页面取数_一页列得下只写条数(client, few):
    out = run_page(client, few["resident"], _STEPS)
    assert out["total"] == 3 and len(out["rows"]) == 3
    assert [r[1] for r in out["rows"]] == ["booked", "booked", "cancelled"]
    assert '<div class="sec-title">我的预约（3）</div>' in out["html"]
    assert "待就诊的全部列出" not in out["html"]
