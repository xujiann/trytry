"""耗材台账与灭菌批次两页的可操作行不再只看最新一页（P2-1358，第四十批扫描 AD1-5）。

- 物资页 `renderMaterials` 原先只取 `/api/materials/consumables` 的缺省一页（最新 100 件、按登记倒序），「使用登记」只挂在
  这一页上：登记满 100 件之后，更早入库、先到效期的在库耗材在页面上登记不了——要么不登记直接用（召回时漏人），要么放到
  过期。扫描实测 101 件：页面 100 行、X-Total-Count 101，最早那件 OLD-0001 不在页面里，接口按 `status=in_stock` 翻得到。
- 消毒供应页 `renderCssd` 原先只取 `/api/cssd/batches`（接口硬截最新 200 个），「发放」「回收」按钮与「以批次响应」的可用
  批次只在这 200 个里：之后又建了 200 个批次，更早灭菌好的那批就点不到，下拉报「没有已完成灭菌的批次可响应」。扫描实测
  页面算出可响应批次 0，接口按 `status=sterile` 取得到 OLD-01。

修法同 P2-456 / P2-457（core.js `actionableFirst`）：在库耗材按状态续页取全（shared.js `fetchAllPages`），已灭菌 / 已发放的
批次按状态单独取，都排在最前、按 id 去重；可响应批次按合并后的算。这里把两页取数那一段原样拿到 node 里跑，请求转给真接口。
"""
import json
import os
import re
import shutil
import subprocess
from datetime import timedelta

import pytest

from conftest import business_today

from app.database import SessionLocal

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面的取数")


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _fetch_block(filename: str, name: str, end_marker: str) -> str:
    """页面函数里取数的那一段：从第一个 `const [`（`await Promise.all` 的解构）到渲染之前的 `end_marker`。"""
    source = _read(filename)
    start = source.index(f"async function {name}()")
    begin = source.index("const [", start)
    end = source.index(end_marker, begin)
    assert end < source.find("\nasync function ", start + 1), f"{name} 里找不到 {end_marker!r}"
    return source[begin:end]


def _actionable_first() -> str:
    found = re.search(r"\nfunction actionableFirst\(.*?\n}\n", _read("core.js"), re.S)
    assert found, "core.js 里找不到 actionableFirst"
    return found.group(0)


def _run_page_fetch(client, headers, block: str, result: str):
    """在 node 里原样执行页面取数的那一段（先加载 shared.js 与 core.js 的 `actionableFirst`），页面的 `api` 经管道转给
    真接口。`Promise.all` 里的几个请求会先后写出、按写出的顺序逐个应答。返回 (`result` 表达式的值, 依次请求过的地址)。"""
    script = (
        # shared.js 顶层只碰 document.addEventListener（原生提交兜底）
        "globalThis.document = { addEventListener() {}, querySelector() { return null; }, cookie: '' };\n"
        + _read("shared.js") + _actionable_first()
        + "\nconst rl = require('readline').createInterface({ input: process.stdin });\n"
        "const lines = rl[Symbol.asyncIterator]();\n"
        "async function api(path) {\n"
        "  process.stdout.write(JSON.stringify({ get: path }) + '\\n');\n"
        "  return JSON.parse((await lines.next()).value);\n"
        "}\n"
        f"(async () => {{\n{block}\n"
        f"  process.stdout.write(JSON.stringify({{ result: {result} }}) + '\\n'); rl.close(); }})();\n"
    )
    proc = subprocess.Popen(["node", "-e", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    requested = []
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{block}"
            message = json.loads(line)
            if "result" in message:
                return message["result"], requested
            requested.append(message["get"])
            assert len(requested) <= 20, f"请求停不下来：{requested}"
            resp = client.get(message["get"], headers=headers)
            assert resp.status_code == 200, (message["get"], resp.text)
            proc.stdin.write(json.dumps(resp.json()) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P21358 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]


def test_耗材登记满100件后_最早入库的在库耗材仍在页面数据里(client, admin, org):
    from app.models import HighValueConsumable

    today = business_today()
    with SessionLocal() as db:   # 最早入库、30 天后到期的一件，之后又登记了 100 件（逐个走接口太慢，直接落库）
        db.add(HighValueConsumable(barcode="P21358-OLD-0001", name="冠脉支架", org_id=org, batch_no="S-2401",
                                   expire_date=(today + timedelta(days=30)).isoformat()))
        db.flush()
        db.add_all([HighValueConsumable(barcode=f"P21358-NEW-{i:04d}", name="导管", org_id=org, batch_no="C-2409",
                                        expire_date=(today + timedelta(days=700)).isoformat()) for i in range(100)])
        db.commit()
    first_page = client.get("/api/materials/consumables", headers=admin)
    assert first_page.headers["X-Total-Count"] == "101"
    assert "P21358-OLD-0001" not in [c["barcode"] for c in first_page.json()]   # 缺省一页取不到它

    block = _fetch_block("pages-mgmt.js", "renderMaterials", "\n  const canApprove")
    got, requested = _run_page_fetch(client, admin, block, "consumables.map((c) => [c.barcode, c.status])")
    rows = dict(got)
    assert rows.get("P21358-OLD-0001") == "in_stock", requested   # 修前页面 100 行、它不在：「使用登记」够不着
    assert len(got) == len(rows) == 101, requested                 # 按 id 去重：在库的与最新一页不重复成两行


def test_灭菌批次超过200个后_更早灭菌好与已发放的批次仍在页面数据里_可响应批次按合并后的算(client, admin, org):
    from app.models import SterilizationBatch

    with SessionLocal() as db:   # 更早灭菌好待发放的一批、已发放待回收的一批，之后又建了 200 个灭菌中的批次
        db.add_all([
            SterilizationBatch(batch_no="P21358-OLD-S", center_org_id=org, item_name="换药包", quantity=20,
                               status="sterile"),
            SterilizationBatch(batch_no="P21358-OLD-D", center_org_id=org, item_name="缝合包", quantity=10,
                               status="dispatched", dispatched_to_org_id=org),
        ])
        db.flush()
        db.add_all([SterilizationBatch(batch_no=f"P21358-NEW-{i:03d}", center_org_id=org, item_name="手术包",
                                       quantity=5) for i in range(200)])
        db.commit()
    latest = [b["batch_no"] for b in client.get("/api/cssd/batches", headers=admin).json()]
    assert len(latest) == 200 and not {"P21358-OLD-S", "P21358-OLD-D"} & set(latest)   # 缺省一页取不到这两批

    block = _fetch_block("core.js", "renderCssd", '\n  $("#page-body").innerHTML')
    got, requested = _run_page_fetch(client, admin, block, "{ batches: batches.map((b) => b.batch_no), "
                                                           "usable: usable.map((b) => b.batch_no) }")
    assert {"P21358-OLD-S", "P21358-OLD-D"} <= set(got["batches"]), requested   # 修前不在：「发放」「回收」够不着
    assert sorted(got["usable"]) == ["P21358-OLD-D", "P21358-OLD-S"], requested   # 修前 0 个：下拉报没有可用批次
    assert len(got["batches"]) == len(set(got["batches"])) == 202, requested    # 按 id 去重
