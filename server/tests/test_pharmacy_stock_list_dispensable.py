"""库存表对每一行「账面与可发不等」都注明可发量（P2-1360，第四十批扫描 AD1-11）。

`docs/用户手册.md` 写「账面数与可发量不一致时，库存表与采购建议会在数量后注明（可发 N）」（P2-1250 写下的口径）。修前
`GET /api/pharmacy/stocks` 不给可发量，管理端药房页只能拿缺药预警（`/alerts`）返回的行注明——阈值 0 的行（批次入库、
调入、验收新建的库存行都是 0）与可发仍高于阈值的行只印账面数。扫描实测卫生院格列齐特库存 100、阈值 0，其中 75 已过期：
缺药预警空，库存表只写 100，县里据此不会往这里调拨。

修后 `/stocks` 的行末尾只增 `dispensable_quantity`（与缺药预警同一个可发量构造：`dispense.dispensable_by_drug` 左连、
一条分组查询），页面对账面与可发不等的每一行注明「（可发 N）」，相等的不注明。
"""
import json
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import event

from conftest import business_today

from app.database import engine

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
#: 修前 `/stocks` 行的键与次序：只许在末尾加一个 `dispensable_quantity`
STOCK_KEYS = ["org_id", "drug_code", "drug_name", "quantity", "threshold", "id"]


def _in_days(n: int) -> str:
    return (business_today() + timedelta(days=n)).isoformat()


def _batch(client, admin, org, code, batch_no, expire, quantity):
    resp = client.post("/api/pharmacy/batches", headers=admin, json={
        "org_id": org, "drug_code": code, "drug_name": f"{code} 片", "batch_no": batch_no,
        "expire_date": expire, "quantity": quantity})
    assert resp.status_code == 201, resp.text


def _stock_row(client, admin, org, code, quantity=0, threshold=0):
    resp = client.post("/api/pharmacy/stocks", headers=admin, json={
        "org_id": org, "drug_code": code, "drug_name": f"{code} 片", "quantity": quantity, "threshold": threshold})
    assert resp.status_code == 200, resp.text


@pytest.fixture(scope="module")
def town(client, admin):
    """卫生院四味药：
    - P21360-GLI：75 片已过期 + 25 片可发，阈值 0（不进缺药预警）——扫描现场；
    - P21360-ALL：40 片全部可发；
    - P21360-LOW：30 片已过期 + 10 片可发，阈值 20（缺药行，修前就注明）；
    - P21360-NIL：建档没入库，一个批次都没有（可发量按 0 算）。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21360 乡卫生院", "org_type": "township", "level": "township"}).json()["id"]
    _batch(client, admin, org, "P21360-GLI", "X1", _in_days(-3), 75)
    _batch(client, admin, org, "P21360-GLI", "X2", _in_days(300), 25)
    _batch(client, admin, org, "P21360-ALL", "A1", _in_days(300), 40)
    _stock_row(client, admin, org, "P21360-LOW", threshold=20)
    _batch(client, admin, org, "P21360-LOW", "L1", _in_days(-1), 30)
    _batch(client, admin, org, "P21360-LOW", "L2", _in_days(100), 10)
    _stock_row(client, admin, org, "P21360-NIL")
    return org


def test_库存行末尾只增可发量_过期批次占一部分的行给出正确的可发量(client, admin, town):
    rows = client.get(f"/api/pharmacy/stocks?org_id={town}", headers=admin).json()
    assert [list(r) for r in rows] == [[*STOCK_KEYS, "dispensable_quantity"]] * 4   # 原有键与次序不动
    got = {r["drug_code"]: (r["quantity"], r["threshold"], r["dispensable_quantity"]) for r in rows}
    assert got == {"P21360-ALL": (40, 0, 40), "P21360-GLI": (100, 0, 25),   # 修前没有这个键
                   "P21360-LOW": (40, 20, 10), "P21360-NIL": (0, 0, 0)}
    assert [r["drug_code"] for r in rows] == ["P21360-ALL", "P21360-GLI", "P21360-LOW", "P21360-NIL"]   # 次序照旧
    # 与缺药预警同一个可发量：缺药行两处数字一致；阈值 0 的那行照旧不进预警
    alerts = {a["drug_code"]: a["dispensable_quantity"] for a in
              client.get(f"/api/pharmacy/alerts?org_id={town}", headers=admin).json()}
    assert alerts == {"P21360-LOW": 10}


def _pharmacy_page_block() -> str:
    """管理端药房页取数到 `stockQty` 定义那一段（`await Promise.all` 的解构起，到批次状态表之前）。"""
    with open(os.path.join(STATIC, "core.js"), encoding="utf-8") as fh:
        source = fh.read()
    start = source.index("async function renderPharmacy()")
    begin = source.index("const [", start)
    return source[begin:source.index("\n  const BATCH_STATUS", begin)]


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面的取数")
def test_页面对账面与可发不等的每一行注明可发量_相等的不注明(client, admin, town):
    block = _pharmacy_page_block()
    responses = {}
    for path in re.findall(r'\bapi\("([^"]+)"\)', block):
        resp = client.get(path, headers=admin)
        assert resp.status_code == 200, (path, resp.text)
        responses[path] = resp.json()
    with open(os.path.join(STATIC, "shared.js"), encoding="utf-8") as fh:
        shared = fh.read()
    script = (
        "globalThis.document = { addEventListener() {}, querySelector() { return null; }, cookie: '' };\n"
        + shared
        + f"\nconst RESPONSES = {json.dumps(responses, ensure_ascii=False)};\n"
        "async function api(path) {\n"
        "  if (!(path in RESPONSES)) throw new Error(`没料到的请求：${path}`);\n"
        "  return JSON.parse(JSON.stringify(RESPONSES[path]));\n"
        "}\n"
        f"(async () => {{\n{block}\n"
        "  process.stdout.write(JSON.stringify(stocks.map((s) => [s.drug_code, stockQty(s)])));\n})();\n"
    )
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    cells = dict(json.loads(done.stdout))
    assert cells == {
        "P21360-GLI": "100（可发 25）",   # 修前只印 100：阈值 0，不在缺药预警里
        "P21360-ALL": "40",               # 全部可发：不注明
        "P21360-LOW": "40（可发 10）",    # 缺药行：修前修后都注明
        "P21360-NIL": "0",
    }


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


def test_查询数不随库存行数增长(client, admin):
    """可发量一条分组查询算出、左连到库存行上，不逐行再查批次：2 行与 12 行的库存表 SQL 条数一样。"""
    orgs = {}
    for size in (2, 12):
        org = client.post("/api/organizations", headers=admin, json={
            "name": f"P21360 {size} 行卫生院", "org_type": "township", "level": "township"}).json()["id"]
        for i in range(size):
            _batch(client, admin, org, f"P21360-Q{i:02d}", f"Q{i:02d}", _in_days(30 + i), 5 + i)
        orgs[size] = org
    counts = {}
    for size, org in orgs.items():
        client.get(f"/api/pharmacy/stocks?org_id={org}", headers=admin)   # 先热一遍（登录态、元数据）
        with _count_sql() as counter:
            rows = client.get(f"/api/pharmacy/stocks?org_id={org}", headers=admin).json()
        assert [r["dispensable_quantity"] for r in rows] == [5 + i for i in range(size)]
        counts[size] = counter["n"]
    assert counts[2] == counts[12], counts
