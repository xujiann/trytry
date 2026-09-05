"""物资采购验收"只能验一次"（P1-30，asset_movements）。

`asset_movements` 本身是**按次多行的台账**（同一物资的入库/领用/归还/报废各成一行，
`admin_mgmt.create_asset_movement` 天天这么写），表上没有任何列能表达
"这是采购单 X 的那次验收"，所以唯一性长不到本表上——它长在父行：

    一张 material_purchases 只能从 contracted 迁出一次（→ received）。

"一张采购单只落一条验收入库流水"只是那条不变式的推论。旧写法
"读 status → 判 contracted → add_amount → 写流水 → 置 received → commit"是
check-then-act：两笔验收同时到达都读到 contracted，库存按同一张单加两次、
写出两条一模一样的 `采购验收 {contract_no}` 流水，事后连哪条是真的都分不出来。

本文件钉四件事：
- **顺序语义一字不变**：第二次验收仍是 409 `当前状态 received 不可验收`，
  未签合同的快路径措辞照旧，超量验收仍是 422 **且不改状态**；
- **并发下恰一路验到**：八路并发只有一路 200，库存与入库流水都只加一次；
- **绕开接口层直调闸门也拦得住**：SQLite 的库级写锁让线程探针对拆卸不敏感，
  直调 `_mark_received` 两次才是确定性的兜底证据；
- **修法不被拆掉**：状态条件必须压在 UPDATE 的 WHERE 里，不许改回 ORM 赋值。
"""
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import login, reset_database

from app.main import app

MATERIALS = Path(__file__).resolve().parents[1] / "app" / "routers" / "materials.py"


@pytest.fixture(scope="module")
def client():
    """raise_server_exceptions=False：并发下要断言的正是"会不会出 500"，
    让异常抛进用例就看不到状态码了（同 test_stage14_concurrency）。"""
    reset_database()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "验收演示县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def roles(client, admin, org):
    """经办报申请、管理层审批——申请人不得自批，所以两个账号都要，且都挂本机构
    （写接口按"只能以本机构名义写"校验）。"""
    for username, role in [("mat_op", "operator"), ("mat_dir", "director")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "passw0rd1", "full_name": username,
                  "role": role, "org_id": org["id"]},
            headers=admin,
        )
    return {"operator": login(client, "mat_op", "passw0rd1"),
            "director": login(client, "mat_dir", "passw0rd1")}


@pytest.fixture(scope="module")
def supplier(client, admin):
    return client.post(
        "/api/pharmacy/suppliers", json={"name": "验收演示供应商", "contact": "赵经理"},
        headers=admin,
    ).json()


def _purchase(client, roles, org, item_name, quantity=20):
    """建一张待审批的采购申请（经办名义）。"""
    resp = client.post(
        "/api/materials/purchases",
        json={"org_id": org["id"], "item_name": item_name, "spec": "标准件", "unit": "个",
              "quantity": quantity, "estimated_price": 100, "reason": "并发验收用例"},
        headers=roles["operator"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _contracted(client, roles, org, supplier, item_name, contract_no, quantity=20):
    """走到 contracted 的采购单：申请（经办）→ 审批（管理层）→ 签合同（经办）。"""
    pid = _purchase(client, roles, org, item_name, quantity)
    assert client.post(
        f"/api/materials/purchases/{pid}/approve", json={"approved": True},
        headers=roles["director"],
    ).status_code == 200
    assert client.post(
        f"/api/materials/purchases/{pid}/contract",
        json={"supplier_id": supplier["id"], "contract_no": contract_no,
              "contract_amount": 100 * quantity},
        headers=roles["operator"],
    ).status_code == 200
    return pid


def _purchase_row(client, roles, org, pid):
    rows = client.get(f"/api/materials/purchases?org_id={org['id']}", headers=roles["operator"]).json()
    return next(r for r in rows if r["id"] == pid)


# ================================================================ 顺序语义


def test_第二次验收仍是409且文案不变(client, admin, roles, org, supplier):
    """并发抢输者拿到的 409 必须与顺序第二次请求**一模一样**——否则调用方分得出
    "是我慢了"还是"系统换了套说法"，那就是行为变更。"""
    pid = _contracted(client, roles, org, supplier, "输液架", "HT-ONCE")
    first = client.post(
        f"/api/materials/purchases/{pid}/receive",
        json={"received_quantity": 20, "note": "验收合格"}, headers=roles["operator"],
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "received" and body["asset_quantity"] == 20
    assert list(body) == ["id", "status", "asset_id", "asset_quantity"], "回执键序不得变"

    again = client.post(
        f"/api/materials/purchases/{pid}/receive",
        json={"received_quantity": 20, "note": "重复验收"}, headers=roles["operator"],
    )
    assert again.status_code == 409
    assert again.json()["detail"] == "当前状态 received 不可验收"

    # 重复的那一路一行也不许落：流水恰一条，库存恰加一次
    movements = client.get(f"/api/mgmt/assets/{body['asset_id']}/movements", headers=admin).json()
    inbound = [m for m in movements if m["movement_type"] == "inbound"]
    assert len(inbound) == 1 and inbound[0]["quantity"] == 20, movements
    assert inbound[0]["note"] == "采购验收 HT-ONCE"
    asset = next(
        a for a in client.get(f"/api/mgmt/assets?org_id={org['id']}", headers=admin).json()
        if a["id"] == body["asset_id"]
    )
    assert asset["quantity"] == 20
    # 验收量与备注仍由本次验收写入（现在随条件 UPDATE 一起落库）
    row = _purchase_row(client, roles, org, pid)
    assert row["status"] == "received" and row["received_quantity"] == 20


def test_未签合同不可验收_快路径措辞照旧(client, roles, org):
    """Python 预检仍是快路径：还没签合同就验收，措辞按**当时的真实状态**报。"""
    pid = _purchase(client, roles, org, "未签合同的架子")
    resp = client.post(
        f"/api/materials/purchases/{pid}/receive", json={"received_quantity": 1},
        headers=roles["operator"],
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "当前状态 requested 不可验收"

    assert client.post(
        f"/api/materials/purchases/{pid}/approve", json={"approved": True},
        headers=roles["director"],
    ).status_code == 200
    resp = client.post(
        f"/api/materials/purchases/{pid}/receive", json={"received_quantity": 1},
        headers=roles["operator"],
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "当前状态 approved 不可验收"


def test_超量验收422且不改状态_随后仍可正常验收(client, roles, org, supplier):
    """闸门必须排在 422 之后：排在前面的话，超量验收会先把单据翻成 received 再报错，
    错误路径上悄悄改了状态，这张单从此再也验不了。"""
    pid = _contracted(client, roles, org, supplier, "超量验收架", "HT-OVER")
    over = client.post(
        f"/api/materials/purchases/{pid}/receive", json={"received_quantity": 25},
        headers=roles["operator"],
    )
    assert over.status_code == 422 and over.json()["detail"] == "验收数量不得超过采购数量"
    assert _purchase_row(client, roles, org, pid)["status"] == "contracted", "错误路径不得改状态"

    ok = client.post(
        f"/api/materials/purchases/{pid}/receive",
        json={"received_quantity": 20, "note": "改按合同量验收"}, headers=roles["operator"],
    )
    assert ok.status_code == 200 and ok.json()["status"] == "received"


# ================================================================ 并发与兜底


def test_八路并发验收恰一路成功_库存与流水都只加一次(client, admin, roles, org, supplier):
    """验收是往库存里加数的路径：闸门先判后改，八笔并发就按同一张单加八次。

    栅栏放行才算真并发——只起线程不够，线程创建本身有先后，前一个常常已提交完了
    后一个才开始读，竞态窗口根本没打开（同 test_stage14_concurrency._race）。
    """
    pid = _contracted(client, roles, org, supplier, "并发验收架", "HT-RACE", quantity=20)

    results: list[tuple[int, dict]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def run():
        barrier.wait(timeout=30)
        resp = client.post(
            f"/api/materials/purchases/{pid}/receive",
            json={"received_quantity": 20, "note": "验收合格"}, headers=roles["operator"],
        )
        with lock:
            results.append((resp.status_code, resp.json()))

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    codes = sorted(code for code, _ in results)
    assert 500 not in codes, f"并发验收不得出 500：{results}"
    assert codes.count(200) == 1, f"同一张采购单被验收 {codes.count(200)} 次：{results}"
    assert codes.count(409) == 7, f"抢输的七路都该拿 409：{results}"
    losers = [body for code, body in results if code == 409]
    assert all(b["detail"] == "当前状态 received 不可验收" for b in losers), losers

    asset_id = next(body for code, body in results if code == 200)["asset_id"]
    movements = client.get(f"/api/mgmt/assets/{asset_id}/movements", headers=admin).json()
    inbound = [m for m in movements if m["movement_type"] == "inbound"]
    assert len(inbound) == 1 and inbound[0]["quantity"] == 20, f"入库流水应恰一条：{movements}"
    asset = next(
        a for a in client.get(f"/api/mgmt/assets?org_id={org['id']}", headers=admin).json()
        if a["id"] == asset_id
    )
    assert asset["quantity"] == 20, "库存只能按这张单加一次"
    row = _purchase_row(client, roles, org, pid)
    assert row["status"] == "received" and row["received_quantity"] == 20


def test_验收后手工再入库仍然合法_台账按次多行(client, admin, roles, org, supplier):
    """反面钉：闸门守的是父行那次跃迁，不是 `asset_movements` 这张台账。

    验收生成的 `MP{id:06d}` 在 `admin_mgmt.create_asset_movement` 眼里就是普通物资，
    事后手工补入库/领用都是正常业务。谁要是顺手给这张表加个
    `(asset_id, movement_type='inbound')` 的唯一索引，这条会红——那是把一条良性的
    多行台账换成"再也补不了库"。
    """
    pid = _contracted(client, roles, org, supplier, "可再入库的架子", "HT-MULTI", quantity=10)
    received = client.post(
        f"/api/materials/purchases/{pid}/receive",
        json={"received_quantity": 10, "note": "验收合格"}, headers=roles["operator"],
    )
    assert received.status_code == 200, received.text
    asset_id = received.json()["asset_id"]

    manual = client.post(
        f"/api/mgmt/assets/{asset_id}/movements",
        json={"movement_type": "inbound", "quantity": 5, "note": "手工补入库"},
        headers=roles["operator"],
    )
    assert manual.status_code == 201, manual.text
    assert manual.json()["asset_quantity"] == 15
    movements = client.get(f"/api/mgmt/assets/{asset_id}/movements", headers=admin).json()
    assert len(movements) == 2, f"手工出入库是按次多行的台账，不该被拦：{movements}"


def test_绕开接口层直调闸门_第二次验不到(client, roles, org, supplier):
    """索引/闸门"在不在"与"拦不拦得住"是两回事。

    接口层的预检在顺序请求下就先给出 409，行为用例因此**分辨不出**兜底是否真的
    生效；SQLite 的库级写锁又让线程探针对拆卸不敏感。这里绕开接口层直调
    `_mark_received`——那正是并发抢输者实际到达的位置——看那条 WHERE 自己抬不抬手。
    """
    from app.database import SessionLocal
    from app.models import MaterialPurchase
    from app.routers.materials import _mark_received

    pid = _contracted(client, roles, org, supplier, "直调闸门架", "HT-DIRECT")
    db = SessionLocal()
    try:
        assert _mark_received(db, pid, 20, "第一路") is True
        db.commit()
        # 闸门被拆成无条件 UPDATE 的话，这一行会变成 True，验收量/备注被第二路盖掉
        assert _mark_received(db, pid, 20, "第二路") is False, "contracted 已经迁出，第二路不该验到"
        db.rollback()
        row = db.get(MaterialPurchase, pid)
        assert row is not None
        assert row.status == "received"
        assert row.received_note == "第一路", f"备注只能是赢家的那条：{row.received_note!r}"
    finally:
        db.close()


# ================================================================ 防拆卸静态钉


def test_验收闸门不得被拆掉_静态钉():
    """谁把状态迁移改回 `purchase.status = "received"` 的 ORM 赋值，这里先红。"""
    source = MATERIALS.read_text(encoding="utf-8")
    assert "def _mark_received(" in source, "验收闸门函数没了"
    assert "update(MaterialPurchase)" in source, "状态迁移必须走条件 UPDATE"
    assert 'MaterialPurchase.status == "contracted"' in source, "状态条件必须压在 UPDATE 的 WHERE 里"
    assert 'purchase.status = "received"' not in source, "ORM 赋值是 check-then-act，不许改回去"
    assert "purchase.received_quantity =" not in source, "验收量应随条件 UPDATE 一起落库"
    assert source.count("if not _mark_received(") == 1, "rowcount 为 0 必须回滚并 409，不许忽略返回值"
