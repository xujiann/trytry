"""「采购与盘点」页只取最新 200 张采购单：更早的待审批、已审批待验收的单子连同「批准 / 驳回」「验收入库」一起从页面消失，
盘点记录同样只列最新 200 条、不提示截断（P2-1671，第四十九批扫描 AM2-1）。

- `renderProcure` 原先 `api("/api/pharmacy/purchase-orders")`、`api("/api/pharmacy/stock-takes")` 不带分页、不读总数，接口缺省
  只回最新 200 张（按编号倒序）。药品采购单一张一个品种，县药房两三个月就过 200 张：已审批、供应商拖着没到货的单子到货时
  页面上找不到「验收入库」，只能走批次入库，这张单永远停在「已审批」；早期的待审批单没人批得了。扫描实测 203 张、最早一张
  已审批：页面那一句 200 行、X-Total-Count 203，最早的已审批单与第二张待审批单都不在页面数据里；盘点 210 条，页面 200 行。

修法照物资页 P2-1506：待审批 / 已审批两态按状态（`list_purchases` 早有可选 `status`）`fetchAllPages` 续页取全、
`actionableFirst` 排在最前、按 id 去重，已驳回 / 已验收的照旧只取最新一页；标题写明「其余只列最新 N 张」。盘点记录只增不减、
没有待办态，不续页取全，经 `api(…, { withTotal: true })` 读 X-Total-Count，列不全时标题写「已列 N / 共 total」（同 P2-1547）。
页面取数那一段原样拿到 node 里跑，`api()` 用 core.js 的原文、`fetch` 经管道转给真接口（响应头照真接口给）。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import login

from app.database import SessionLocal

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面的取数")


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _function_source(source: str, head: str) -> str:
    """顶层函数原文：从 `head` 到它之后第一个顶格的 `}`。"""
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def _fetch_block() -> str:
    """`renderProcure` 取数那一段：从 `await Promise.all` 的解构起，到渲染要用的 `role` 之前。"""
    source = _read("pages-public.js")
    start = source.index("async function renderProcure()")
    begin = source.index("const [", start)
    return source[begin:source.index("\n  const role = currentRole();", begin)]


PRELUDE = r"""
globalThis.document = { addEventListener() {}, querySelector() { return null; }, cookie: "" };
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
/* 真 `api()` 要的几样：迁移期令牌（空 = Cookie 模式）、CSRF、登出（走到就是用例写错了） */
let token = "";
function csrfToken() { return ""; }
function logout() { throw new Error("不该走到登出"); }
/* 浏览器的 fetch：经管道转给真接口，响应头只垫 X-Total-Count */
globalThis.fetch = async (path) => {
  process.stdout.write(JSON.stringify({ get: path }) + "\n");
  const reply = JSON.parse((await lines.next()).value);
  return { status: 200, ok: true, json: async () => reply.body,
    headers: { get: (name) => (name.toLowerCase() === "x-total-count" ? reply.total : null) } };
};
"""


def _run_page_fetch(client, headers, result: str):
    """在 node 里原样执行取数那一段，返回 (`result` 表达式的值, 依次请求过的地址)。"""
    core = _read("core.js")
    script = (
        PRELUDE + _read("shared.js") + "\n"
        + _function_source(core, "async function api(") + _function_source(core, "function actionableFirst(")
        + f"(async () => {{\n{_fetch_block()}\n"
        f"  process.stdout.write(JSON.stringify({{ result: {result} }}) + '\\n'); rl.close(); }})();\n"
    )
    proc = subprocess.Popen(["node", "-e", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    requested = []
    try:
        while True:
            line = proc.stdout.readline()
            assert line, "node 没给出结果就退出了"
            message = json.loads(line)
            if "result" in message:
                return message["result"], requested
            requested.append(message["get"])
            assert len(requested) <= 20, f"请求停不下来：{requested}"
            resp = client.get(message["get"], headers=headers)
            assert resp.status_code == 200, (message["get"], resp.text)
            reply = {"body": resp.json(), "total": resp.headers.get("x-total-count")}
            proc.stdin.write(json.dumps(reply, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


#: 两个标题用的数在修前没有：取不到就回 null，让断言报「不在页面数据里 / 标题没写」而不是 node 里一个 ReferenceError
RESULT = ("{ rows: orders.map((o) => [o.id, o.status]), takeIds: takes.map((t) => t.id),"
          " orderCount: typeof orderCount === 'undefined' ? null : orderCount,"
          " takeCount: typeof takeCount === 'undefined' ? null : takeCount }")


@pytest.fixture(scope="module")
def world(client, admin):
    from app.models import DrugStock, Supplier

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21671 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ids = {}
    for username, role in (("p21671_ph", "pharmacist"), ("p21671_dir", "director")):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pw123456", "role": role, "org_id": org})
        assert created.status_code == 201, created.text
        ids[username] = created.json()["id"]
    with SessionLocal() as db:
        supplier = Supplier(name="P21671 国药")
        db.add(supplier)
        db.add(DrugStock(org_id=org, drug_code="P21671-ST", drug_name="P21671 盘点药", quantity=0, threshold=0))
        db.commit()
        supplier_id = supplier.id
    return {"org": org, "ids": ids, "supplier": supplier_id,
            **{name: login(client, name, "pw123456") for name in ids}}


def test_采购满200张后_更早的待审批与待验收单仍在页面数据里且排在最前_标题写明截断(client, world):
    from app.models import PurchaseOrder

    ph = world["ids"]["p21671_ph"]

    def order(name, status, **extra):
        return PurchaseOrder(org_id=world["org"], supplier_id=world["supplier"], item_code=name, item_name=name,
                             quantity=10, status=status, requested_by=ph, **extra)

    with SessionLocal() as db:   # 最早两张：一张已审批待验收（到货了要点「验收入库」），一张仍待审批（逐张走接口太慢，直接落库）
        early = [order("P21671-OLD-A", "approved", approved_by=world["ids"]["p21671_dir"]),
                 order("P21671-OLD-P", "pending")]
        db.add_all(early)
        db.commit()
        early_ids = {o.id: o.status for o in early}
    before, requested = _run_page_fetch(client, world["p21671_dir"], RESULT)
    assert dict(before["rows"]) == early_ids, requested

    with SessionLocal() as db:   # 之后又办结了 201 张，合计 203 张
        db.add_all([order(f"P21671-NEW-{i:03d}", "received" if i % 2 else "rejected") for i in range(201)])
        db.commit()
    first_page = client.get("/api/pharmacy/purchase-orders", headers=world["p21671_dir"])
    assert first_page.headers["X-Total-Count"] == "203"
    assert not set(early_ids) & {o["id"] for o in first_page.json()}   # 页面原先那一句取不到这两张

    got, requested = _run_page_fetch(client, world["p21671_dir"], RESULT)
    rows = dict(got["rows"])
    # 修前页面 200 行、这两张都不在：待审批的「批准 / 驳回」、待验收的「验收入库」都够不着
    assert {i: rows.get(i) for i in early_ids} == early_ids, requested
    assert {i for i, _ in got["rows"][:2]} == set(early_ids), requested   # 待办的排在最前
    assert len(got["rows"]) == len(rows) == 202, requested                # 按 id 去重，已办结的照旧是最新一页
    # 修前标题不带数，看不出被截断；最新一页没取满时标题照旧是行数
    assert (before["orderCount"], got["orderCount"]) == ("2", "待审批 / 待验收 2 张排在最前、其余只列最新 200 张"), requested
    assert "/api/pharmacy/purchase-orders?status=pending&limit=500&offset=0" in requested
    assert "/api/pharmacy/purchase-orders?status=approved&limit=500&offset=0" in requested


def test_盘点记录过200条_标题写明已列与总数(client, world):
    from app.models import StockTake

    got, _ = _run_page_fetch(client, world["p21671_dir"], RESULT)
    assert (got["takeIds"], got["takeCount"]) == ([], "0")
    with SessionLocal() as db:   # 一次全面盘点 210 个品种（直接落库：盘点接口要逐个建库存行）
        db.add_all([StockTake(org_id=world["org"], drug_code="P21671-ST", book_qty=0, actual_qty=0, diff=0,
                              note=f"说明{i}", created_by=world["ids"]["p21671_ph"]) for i in range(210)])
        db.commit()
    got, requested = _run_page_fetch(client, world["p21671_dir"], RESULT)
    assert len(got["takeIds"]) == 200, requested
    # 修前页面 200 行、标题不带数，看着像一共只盘了 200 条
    assert got["takeCount"] == "已列 200 / 共 210", requested
