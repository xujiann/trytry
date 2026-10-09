"""采购单、盘点记录不出经手人与时间，说明字段只写不读（P2-1673，第四十九批扫描 AM2-9）。

- `PurchaseOrderOut` 原先只有 id / 机构 / 供应商 / 品目 / 数量 / 状态 / 实收数：申请人（`requested_by`）、审批人（`approved_by`）、
  申请时间（`created_at`）、申请说明（`note`）库里都存着，任何接口都不返回；`StockTakeOut` 没有盘点人（`created_by`）与时间。
  盘亏 120 盒是谁、哪天盘的，页面与清单接口都答不出，只能翻请求级审计日志；盘点表单里填的「差异说明」页面上也不印。
  扫描实测（`r2_po_page.py`）采购单清单行键 `['id','org_id','supplier_id','item_type','item_code','item_name','quantity',
  'status','received_quantity']`，盘点清单行键里没有人和时间。

修法：两个清单出参只在末尾追加——采购单 `requested_by_name` / `approved_by_name` / `created_at` / `note`，盘点
`created_by_name` / `created_at`。显示名按页一次取齐（同发药记录 P2-1539：姓名，没填姓名的回落账号），还没审批的为空串；
时间照同文件 `BatchDispenseRow.dispensed_at` 的写法（`isoformat()`）。页面采购单表加申请人 / 审批人 / 申请时间 / 备注列，
盘点表加差异说明 / 盘点人 / 盘点时间列，一律 `esc()`。不加 `requested_by_me`、不改审批按钮的显示（那是 P2-447 待裁定）。
页面那几条把 `renderProcure` 原文放进 node 跑，`api()` 用 core.js 的原文、`fetch` 经管道转给真接口。
"""
import json
import re
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import event

from conftest import login

from app.database import SessionLocal, engine

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
#: 修前两个清单行的键与次序：只许在末尾追加
ORDER_KEYS = ["id", "org_id", "supplier_id", "item_type", "item_code", "item_name", "quantity", "status",
              "received_quantity"]
NEW_ORDER_KEYS = ["requested_by_name", "approved_by_name", "created_at", "note"]
TAKE_KEYS = ["id", "org_id", "drug_code", "book_qty", "actual_qty", "diff", "note"]
NEW_TAKE_KEYS = ["created_by_name", "created_at"]
NOTE = "急用<b>库存只够三天</b>"
TAKE_NOTE = "破损 <i>2</i> 盒"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.models import DrugStock, PurchaseOrder, StockTake, Supplier

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21673 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    for username, role, full_name in (("p21673_ph", "pharmacist", "P21673 药师甲"),
                                      ("p21673_dir", "director", "P21673 院长"),
                                      ("p21673_op", "operator", "")):   # 经办没填姓名：显示名回落账号
        made = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pw123456", "role": role, "org_id": org, "full_name": full_name})
        assert made.status_code == 201, made.text
    heads = {name: login(client, name, "pw123456") for name in ("p21673_ph", "p21673_dir", "p21673_op")}
    with SessionLocal() as db:
        supplier = Supplier(name="P21673 国药")
        db.add(supplier)
        db.add(DrugStock(org_id=org, drug_code="P21673-AMX", drug_name="P21673 阿莫西林", quantity=0, threshold=0))
        db.commit()
        supplier_id = supplier.id
    approved = client.post("/api/pharmacy/purchase-orders", headers=heads["p21673_ph"], json={
        "org_id": org, "supplier_id": supplier_id, "item_code": "P21673-AMX", "item_name": "P21673 阿莫西林",
        "quantity": 100, "note": NOTE})
    assert approved.status_code == 201, approved.text
    assert client.post(f"/api/pharmacy/purchase-orders/{approved.json()['id']}/approve",
                       headers=heads["p21673_dir"]).status_code == 200
    pending = client.post("/api/pharmacy/purchase-orders", headers=heads["p21673_op"], json={
        "org_id": org, "supplier_id": supplier_id, "item_code": "P21673-INS", "item_name": "P21673 胰岛素",
        "quantity": 20})
    assert pending.status_code == 201, pending.text
    take = client.post("/api/pharmacy/stock-takes", headers=heads["p21673_ph"], json={
        "org_id": org, "drug_code": "P21673-AMX", "actual_qty": 0, "note": TAKE_NOTE})
    assert take.status_code == 201, take.text
    with SessionLocal() as db:
        created = {"approved": db.get(PurchaseOrder, approved.json()["id"]).created_at.isoformat(),
                   "pending": db.get(PurchaseOrder, pending.json()["id"]).created_at.isoformat(),
                   "take": db.get(StockTake, take.json()["id"]).created_at.isoformat()}
    return {"org": org, "heads": heads, "supplier": supplier_id, "approved": approved.json()["id"],
            "pending": pending.json()["id"], "take": take.json()["id"], "created": created}


def test_采购单清单末尾追加申请人审批人申请时间与说明_原键次序不动(client, world):
    rows = {r["id"]: r for r in client.get("/api/pharmacy/purchase-orders", headers=world["heads"]["p21673_dir"],
                                           params={"org_id": world["org"]}).json()}
    assert [list(r) for r in rows.values()] == [ORDER_KEYS + NEW_ORDER_KEYS] * 2   # 修前只有原 9 个键
    assert {k: rows[world["approved"]][k] for k in NEW_ORDER_KEYS} == {
        "requested_by_name": "P21673 药师甲", "approved_by_name": "P21673 院长",
        "created_at": world["created"]["approved"], "note": NOTE}   # 修前申请说明写进去就再也读不出来
    assert {k: rows[world["pending"]][k] for k in NEW_ORDER_KEYS} == {
        "requested_by_name": "p21673_op", "approved_by_name": "",   # 没填姓名回落账号；还没审批为空串
        "created_at": world["created"]["pending"], "note": ""}
    assert rows[world["approved"]]["status"] == "approved" and rows[world["pending"]]["status"] == "pending"


def test_盘点清单末尾追加盘点人与时间_原键次序不动(client, world):
    rows = client.get("/api/pharmacy/stock-takes", headers=world["heads"]["p21673_dir"],
                      params={"org_id": world["org"]}).json()
    assert [list(r) for r in rows] == [TAKE_KEYS + NEW_TAKE_KEYS]   # 修前没有人和时间
    assert {k: rows[0][k] for k in ("id", "note", *NEW_TAKE_KEYS)} == {
        "id": world["take"], "note": TAKE_NOTE, "created_by_name": "P21673 药师甲",
        "created_at": world["created"]["take"]}


@contextmanager
def _count_sql():
    counter = {"n": 0}

    def _tick(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _tick)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _tick)


def test_经手人显示名按页一次取齐_查询数不随行数增长(client, admin, world):
    """申请人、审批人各不相同的单子，一页 1 张与一页 6 张的 SQL 条数一样：显示名一条查询取齐，不逐行查用户。"""
    from app.models import PurchaseOrder, User

    with SessionLocal() as db:   # 六个各不相同的申请人（审批人同样各不相同），直接落库
        users = [User(username=f"p21673_u{i}", password_hash="x", role="operator", org_id=world["org"],
                      full_name=f"P21673 经办{i}") for i in range(12)]
        db.add_all(users)
        db.flush()
        db.add_all([PurchaseOrder(org_id=world["org"], supplier_id=world["supplier"], item_code=f"P21673-N{i}",
                                  item_name=f"P21673 药{i}", quantity=1, status="approved",
                                  requested_by=users[i].id, approved_by=users[6 + i].id) for i in range(6)])
        db.commit()
    counts = {}
    for limit in (1, 6):
        with _count_sql() as counter:
            got = client.get("/api/pharmacy/purchase-orders", headers=admin,
                             params={"org_id": world["org"], "status": "approved", "limit": limit})
        assert got.status_code == 200 and len(got.json()) == limit, got.text
        counts[limit] = counter["n"]
    assert counts[1] == counts[6], counts
    names = [(r["requested_by_name"], r["approved_by_name"]) for r in got.json()]
    assert names == [(f"P21673 经办{i}", f"P21673 经办{6 + i}") for i in range(5, -1, -1)]   # id 倒序


# ---------------------------------------------------------------- 页面：renderProcure 原样拿到 node 里跑
PRELUDE = r"""
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const els = {};
globalThis.localStorage = { getItem: (k) => (k === "medplat_role" ? "director" : null), setItem() {}, removeItem() {} };
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { dataset: {}, innerHTML: "", textContent: "", className: "",
    classList: { add() {}, remove() {} } }); } };
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


def _function_source(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def _render_page(client, headers) -> str:
    """跑一遍 `renderProcure()`，回 `#page-body` 的 innerHTML。"""
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    public = (STATIC / "pages-public.js").read_text(encoding="utf-8")
    script = (
        PRELUDE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n"
        + "".join(_function_source(core, head) for head in (
            "async function api(", "function table(", "function panel(", "function actionableFirst("))
        + re.search(r"^function currentRole\(\).*$", core, re.M).group(0) + "\n"
        + re.search(r"^const PO_STATUS = .*$", public, re.M).group(0) + "\n"
        + _function_source(public, "async function renderProcure(")
        + "(async () => { await renderProcure();\n"
        "  process.stdout.write(JSON.stringify({ result: document.querySelector('#page-body').innerHTML }) + '\\n');"
        " rl.close(); })().catch((e) => { console.error(e); process.exit(1); });\n"
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
            resp = client.get(message["get"], headers=headers)
            assert resp.status_code == 200, (message["get"], resp.text)
            reply = {"body": resp.json(), "total": resp.headers.get("x-total-count")}
            proc.stdin.write(json.dumps(reply, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def _row(html: str, first_cell: int) -> str:
    return re.search(rf"<tr><td>{first_cell}</td>[\s\S]*?</tr>", html).group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面")
def test_页面采购单印申请人审批人申请时间与备注_盘点印差异说明盘点人与时间_一律转义(client, world):
    html = _render_page(client, world["heads"]["p21673_dir"])
    orders = html[html.index("采购申请（经办/药师）"):html.index("存货盘点（经办/药师")]
    takes = html[html.index("存货盘点（经办/药师"):]
    # 修前采购单表只有 ID / 机构 / 供应商 / 类型 / 品目 / 数量 / 状态 / 操作，盘点表没有说明、人和时间
    for col in ("申请人", "审批人", "申请时间", "备注"):
        assert f"<th>{col}</th>" in orders, col
    for col in ("差异说明", "盘点人", "盘点时间"):
        assert f"<th>{col}</th>" in takes, col
    approved = _row(orders, world["approved"])
    assert "<td>P21673 药师甲</td><td>P21673 院长</td>" in approved, approved
    assert f"<td>{world['created']['approved'][:16].replace('T', ' ')}</td>" in approved
    assert "<td>急用&lt;b&gt;库存只够三天&lt;/b&gt;</td>" in approved and "<b>" not in approved   # 说明一律转义
    pending = _row(orders, world["pending"])
    assert "<td>p21673_op</td><td>—</td>" in pending and pending.count("<td>—</td>") == 2, pending   # 没审批、没写说明
    assert f'data-poap="{world["pending"]}"' in pending   # 审批按钮的显示照旧（P2-447 待裁定，本条不动）
    take = _row(takes, world["take"])
    assert "<td>破损 &lt;i&gt;2&lt;/i&gt; 盒</td><td>P21673 药师甲</td>" in take, take   # 修前填了「差异说明」页面上看不到
    assert f"<td>{world['created']['take'][:16].replace('T', ' ')}</td>" in take
