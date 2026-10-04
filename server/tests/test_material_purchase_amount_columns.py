"""物资采购审批时看不到金额：「采购流程」表只有物资、规格、数量、状态、合同号、已验收（P2-1362，第四十批「审核与审批」
扫描 AD2-5 的页面一半）。

清单接口每行本来就带 `estimated_price`（预估单价）与 `contract_amount`（合同金额），页面一个都不画：审批人点「审批」时
连这笔要花多少都看不到——批的是 10 把 × 500 元，表上只有「10把」；签了合同也看不出合同金额与申请时的预估差了多少。

修法：只改页面——表里加「预估单价」「预估总额（数量 × 预估单价）」「合同金额」三列，没填的（预估单价 0、合同还没签）
显示 —，金额照本页会计 / 成本两页的两位小数写法，一律 `esc()`。接口不动；合同金额超出审批额度拦不拦是另一句业务口径
（登记待裁定），本条不做。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import login

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-mgmt.js").read_text(encoding="utf-8")
HELPER = "materialPurchaseAmounts"


def _function(name: str) -> str:
    start = PAGE.index(f"function {name}(")
    return PAGE[start:PAGE.index("\n}\n", start) + 2]


def _purchase_table() -> str:
    """「采购流程」那张表：表头数组到行模板结尾。"""
    start = PAGE.index("panel(`采购流程（")
    return PAGE[start:PAGE.index("</tr>`;", start)]


def _amounts(rows: list[dict]) -> list[dict]:
    """在 node 里跑页面上的取数帮手，逐行回三列的显示值。"""
    script = (_function(HELPER)
              + f"\nconsole.log(JSON.stringify(JSON.parse(process.argv[1]).map((p) => {HELPER}(p))));")
    out = subprocess.run(["node", "-e", script, json.dumps(rows)], capture_output=True, text=True, check=True,
                         timeout=60).stdout
    return json.loads(out)


def test_采购流程表有三列金额_取数走帮手_一律转义():
    table = _purchase_table()
    # 修前表头是 ID / 物资 / 规格 / 数量 / 状态 / 合同 / 已验收 / 操作，审批人看不到这笔要花多少
    assert '"数量", "预估单价", "预估总额", "状态", "合同", "合同金额", "已验收"' in table
    assert f"const amt = {HELPER}(p);" in table
    for key in ("price", "total", "contract"):
        assert f"<td>${{esc(amt.{key})}}</td>" in table


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端函数")
def test_跑一遍取数_没填的显示横线_总额是数量乘单价():
    rows = [{"quantity": 3, "estimated_price": 0.1, "contract_no": "", "contract_amount": 0},
            {"quantity": 2, "estimated_price": 0, "contract_no": "", "contract_amount": 0},
            {"quantity": 1, "estimated_price": 0, "contract_no": "HT-0", "contract_amount": 0}]
    assert _amounts(rows) == [
        {"price": "0.10", "total": "0.30", "contract": "—"},   # 0.1 × 3 不印成 0.30000000000000004
        {"price": "—", "total": "—", "contract": "—"},          # 预估单价没填、合同没签
        {"price": "—", "total": "—", "contract": "0.00"},       # 签了 0 元的合同照写，不当没签
    ]


@pytest.fixture(scope="module")
def rows(client, admin):
    """经办提的两条申请：一条 10 把 × 预估单价 500 元、批了之后签了 50 万的合同（扫描复现的那一单），一条没填预估单价、
    还在待审批。按清单接口原样取回。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21362 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    for username, role in (("p21362_op", "operator"), ("p21362_dir", "director")):
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pw123456", "role": role, "org_id": org})
        assert resp.status_code in (200, 201), resp.text
    op, director = login(client, "p21362_op", "pw123456"), login(client, "p21362_dir", "pw123456")
    supplier = client.post("/api/pharmacy/suppliers", headers=admin, json={"name": "P21362 供应商"}).json()["id"]
    chair = client.post("/api/materials/purchases", headers=op, json={
        "org_id": org, "item_name": "P21362 办公椅", "unit": "把", "quantity": 10, "estimated_price": 500})
    assert chair.status_code == 201, chair.text
    approved = client.post(f"/api/materials/purchases/{chair.json()['id']}/approve", headers=director,
                           json={"approved": True})
    assert approved.status_code == 200, approved.text
    signed = client.post(f"/api/materials/purchases/{chair.json()['id']}/contract", headers=op, json={
        "supplier_id": supplier, "contract_no": "P21362-HT-1", "contract_amount": 500000})
    assert signed.status_code == 200, signed.text
    gauze = client.post("/api/materials/purchases", headers=op, json={
        "org_id": org, "item_name": "P21362 纱布", "quantity": 3})
    assert gauze.status_code == 201, gauze.text
    listed = client.get("/api/materials/purchases", headers=director, params={"org_id": org})
    assert listed.status_code == 200, listed.text
    by_name = {r["item_name"]: r for r in listed.json()}
    return [by_name["P21362 办公椅"], by_name["P21362 纱布"]]


def test_清单行本来就带预估单价与合同金额(rows):
    chair, gauze = rows
    assert (chair["quantity"], chair["estimated_price"], chair["contract_no"], chair["contract_amount"]) == (
        10, 500, "P21362-HT-1", 500000)
    assert (gauze["estimated_price"], gauze["contract_no"], gauze["contract_amount"]) == (0, "", 0)


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端函数")
def test_接口原样的行进来_三列照显示(rows):
    assert _amounts(rows) == [
        {"price": "500.00", "total": "5000.00", "contract": "500000.00"},   # 批的是 5000 元的事，签的是 50 万
        {"price": "—", "total": "—", "contract": "—"},
    ]
