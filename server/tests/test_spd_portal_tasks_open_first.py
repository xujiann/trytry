"""居民端「健康任务」页未结束的排在最前、历程卡片标题印任务总数（P2-1674，第四十九批扫描 AM1-1）。

修前：`m.js` 的 `renderSpdTasks` 不带筛选取 `/api/portal/spd/tasks`——接口缺省 `limit=100`、按到期日正序、办完与取消的
照列。老患者办结的任务攒过 100 条，新派的待办被挤出这一页：扫描实测（`am1/r4_task_list_trunc.py`）120 条已办结的周任务
之后再派 1 条，首页「待办任务 1」，任务页那一调回 100 行、X-Total-Count 121、全是「已完成」，新任务不在、没有一个能办的
按钮，页面也不说截断。历程（`portal.journey`）每份档案只回最近 30 条任务，`renderSpdJourney` 把这 30 印成「任务（30）」。

修法：照任务中心 P2-827 在页面上改——未结束的（`service.TASK_OPEN_STATUSES`，含待审核）逐个状态续页取全、排最前，其后接
缺省清单里办结的（列不全时取它的末一页、新的在前），按 id 去重，经 `withTotal` 读 X-Total-Count，列不全时写「最近 N 条
（共 X 条）」；接口缺省排序与字节不动。历程出参每份档案末尾加 `tasks_total`（按档案计的任务总数，与卡片同一口径），卡片
标题列不全时写「任务：最近 30 条（共 N 条）」。

页面那几条把 `m.js` 的 `api()`、取数与渲染函数原文放进 node 跑，`fetch` 经管道转给真接口（以居民本人的身份），响应头照
真接口给——`withTotal` 读到的是真的 X-Total-Count。
"""
import json
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.database import SessionLocal

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
B = "/api/portal/spd"
#: 已办结的老任务条数：比任务清单缺省一页（100）多 20 条
DONE = 120


def _resident(client, admin, n: int, extra: tuple = ()) -> dict:
    """一位高血压在管的居民：两年多里每周一条、共 120 条已办结的上报任务，另加 `extra`（状态, 到期日）几条。"""
    from app.spd.models import SpdTask

    org = client.post("/api/organizations", headers=admin, json={
        "name": f"P21674 卫生院{n}", "org_type": "township", "level": "township"}).json()["id"]
    phone = f"1390001674{n}"
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P21674 居民{n}", "id_card": f"33010219680505167{n}", "gender": "男", "birth_date": "1968-05-05",
        "phone": phone})
    assert patient.status_code == 201, patient.text
    pid = patient.json()["id"]
    enrolled = client.post("/api/spd/enrollments", headers=admin, json={
        "patient_id": pid, "program_code": "hypertension", "org_id": org})
    assert enrolled.status_code == 201, enrolled.text
    start = date(2024, 6, 1)
    rows = [("done", (start + timedelta(days=7 * i)).isoformat()) for i in range(DONE)] + list(extra)
    with SessionLocal() as db:   # 逐条走接口太慢，直接落库
        tasks = [SpdTask(patient_id=pid, enrollment_id=enrolled.json()["id"], program_code="hypertension",
                         title=f"P21674 第{i + 1}次居家血压上报", task_type="report", status=status,
                         due_date=due, org_id=org) for i, (status, due) in enumerate(rows)]
        db.add_all(tasks)
        db.commit()
        extra_ids = [t.id for t in tasks[DONE:]]
    code = client.post("/api/portal/auth/sms/code", json={"phone": phone}).json()["debug_code"]
    resident = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code})
    assert resident.status_code == 200 and resident.json()["bound"], resident.text
    return {"patient": pid, "extra": extra_ids,
            "resident": {"Authorization": f"Bearer {resident.json()['access_token']}"}}


@pytest.fixture(scope="module")
def world(client, admin):
    """扫描的现场：120 条已办结的之后，今天医生再派 1 条（7 天后到期）。"""
    out = _resident(client, admin, 0)
    fresh = client.post("/api/spd/tasks", headers=admin, json={
        "patient_id": out["patient"], "title": "P21674 本周居家血压上报", "task_type": "report",
        "program_code": "hypertension"})
    assert fresh.status_code == 201, fresh.text
    return {**out, "fresh": fresh.json()["id"]}


@pytest.fixture(scope="module")
def old_open(client, admin):
    """另一位：120 条已办结的之外，还有到期日最早的两条没结束——提交了在等审核的、退回要重做的。"""
    return _resident(client, admin, 1, (("submitted", "2024-05-18"), ("rejected", "2024-05-25")))


def test_接口缺省调用照旧_新派的不在缺省那一页(client, world):
    """接口缺省排序与字节不动（修法只改页面）：仍是到期日正序、100 条、办完的照列——页面原先就这一调，新任务看不到。"""
    resp = client.get(f"{B}/tasks", headers=world["resident"])
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert (len(rows), resp.headers["X-Total-Count"]) == (100, str(DONE + 1))
    assert [(r["due_date"], r["id"]) for r in rows] == sorted((r["due_date"], r["id"]) for r in rows)
    assert {r["status"] for r in rows} == {"done"} and world["fresh"] not in [r["id"] for r in rows]
    assert list(rows[0]) == ["id", "title", "task_type", "status", "due_date", "form_code", "require_evidence",
                             "result", "review_note"]
    home = client.get(f"{B}/home", headers=world["resident"]).json()
    assert home["todo"]["tasks"] == 1   # 首页报「待办任务 1」


def test_历程每份档案带任务总数_键在末尾(client, world):
    prog = client.get(f"{B}/journey", headers=world["resident"]).json()["programs"][0]
    assert (len(prog["tasks"]), prog["tasks_total"]) == (30, DONE + 1)   # 修前没有 tasks_total
    assert list(prog)[-1] == "tasks_total"   # 新字段只加在末尾
    assert prog["tasks"][0]["id"] == world["fresh"]   # tasks 仍是最近 30 条（编号倒序）


# ------------------------------------------------------------------ 页面

PRELUDE = r"""
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const requests = [];
globalThis.document = { addEventListener() {}, cookie: "", querySelector() { return null; } };
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
            "async function api(", "function spdQuery(", "function kv(", "function spdTagOf(", "const SPD_RISK_TAGS = ",
            "const SPD_ENROLL_STATUS_TAGS = ", "const SPD_INST_STATUS_TAGS = ", "const SPD_TASK_STATUS_TAGS = ",
            "const SPD_TASK_OPEN_STATUSES = ", "async function fetchSpdTasks(", "async function renderSpdTasks(",
            "async function renderSpdJourney("))
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


def test_页面的未结束状态与后端同一串():
    from app.spd.service import TASK_OPEN_STATUSES

    source = (STATIC / "m" / "m.js").read_text(encoding="utf-8")
    line = _top(source, "const SPD_TASK_OPEN_STATUSES = ")
    assert json.loads(line[line.index("["):line.rindex("]") + 1]) == list(TASK_OPEN_STATUSES)   # 含待审核


def test_页面取数_新派的待办排最前_办结的新的在前_写明列不全(client, world):
    out = run_page(client, world["resident"], """
      const { rows, total } = await fetchSpdTasks();
      await renderSpdTasks(box);
      return { rows: rows.map((t) => [t.id, t.status, t.due_date]), total, requests, html: box.innerHTML };
    """)
    rows, total = out["rows"], out["total"]
    assert rows[0][0] == world["fresh"], rows[:3]   # 修前任务页那一调里根本没有它
    assert total == DONE + 1
    done = [r for r in rows if r[1] == "done"]
    # 缺省清单的末一页（到期最近的 100 条）里也有这条新任务，去重后：未结束的 1 条 + 最近办结的 99 条
    assert len(rows) == 100 and len(done) == 99 and len({r[0] for r in rows}) == 100
    assert [r[2] for r in done] == sorted((r[2] for r in done), reverse=True)   # 办结的新的在前
    assert done[0][2] == (date(2024, 6, 1) + timedelta(days=7 * (DONE - 1))).isoformat()   # 最近办结的那条在
    assert f"{B}/tasks" in out["requests"]   # 缺省清单照旧不带参数取
    assert f"{B}/tasks?status=pending&limit=500&offset=0" in out["requests"]   # 未结束的逐个状态续页取全
    assert "最近 100 条（共 121 条），未结束的排在最前" in out["html"]   # 修前不说截断
    assert f'data-spd-task="{world["fresh"]}"' in out["html"]   # 新任务卡片上有「填报并提交」


def test_页面取数_到期早的未结束任务不在末页也列在最前(client, old_open):
    """未结束的逐个状态取全（含待审核）：到期日最早的两条不在缺省清单的末一页里，照样列在最前、按到期日排。"""
    out = run_page(client, old_open["resident"], """
      const { rows, total } = await fetchSpdTasks();
      await renderSpdTasks(box);
      return { rows: rows.map((t) => [t.id, t.status]), total, html: box.innerHTML };
    """)
    submitted, rejected = old_open["extra"]
    assert out["rows"][:2] == [[submitted, "submitted"], [rejected, "rejected"]], out["rows"][:3]
    assert len(out["rows"]) == 102 and out["total"] == DONE + 2   # 未结束的 2 条 + 最近办结的 100 条
    assert "最近 102 条（共 122 条），未结束的排在最前" in out["html"]
    # 退回的有「填报并提交」，待审核的没有（与修前同一套按钮规矩）
    assert f'data-spd-task="{rejected}"' in out["html"] and f'data-spd-task="{submitted}"' not in out["html"]


def test_历程卡片标题印任务总数(client, world):
    out = run_page(client, world["resident"], """
      await renderSpdJourney(box);
      return [...box.innerHTML.matchAll(/<div class="sec-title">([^<]*)<\\/div>/g)].map((m) => m[1]);
    """)
    assert "任务：最近 30 条（共 121 条）" in out, out   # 修前「任务（30）」
