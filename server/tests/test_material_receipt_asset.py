"""采购验收的到货记到别家机构的物资上：别人手工建一件同编码的物资，就截走了这张单的入库（P2-397）。

验收入账原先按编码 `MP+六位单号` 找物资、找到哪件记哪件；物资建档（`/api/mgmt/assets`）不限编码。乙院经办建一件
MP000001，甲院 1 号采购单验收的 10 件就加到了乙院那件上——回执里是乙院的物资号、「采购验收」流水挂在乙院，甲院
台账一件没有；本院手工建的、已报废的同编码物资同样被加量。而这个编码只有验收这一处会生成，一张单只验收得了一次，
库里已有的同编码物资一定不是这张单建的。修后每次验收都新建一件挂在采购单机构名下的物资，编码被占就带后缀。
"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": f"P2397 {name}", "org_type": "township", "level": "township"}).json()["id"] for name in ("甲院", "乙院")]
    heads = {}
    for username, role, org in (("p2397_op_a", "operator", orgs[0]), ("p2397_dir_a", "director", orgs[0]),
                                ("p2397_op_b", "operator", orgs[1])):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "full_name": username, "role": role, "org_id": org})
        assert created.status_code == 201, created.text
        heads[username] = login(client, username, "passw0rd1")
    supplier = client.post("/api/pharmacy/suppliers", headers=admin,
                           json={"name": "P2397 供应商", "contact": "赵经理"}).json()["id"]
    return {"a": orgs[0], "b": orgs[1], "supplier": supplier, **heads}


def _contracted(client, world, item_name):
    """甲院一张走到已签合同的采购单：经办申请 → 管理层审批 → 经办签合同。"""
    pid = client.post("/api/materials/purchases", headers=world["p2397_op_a"], json={
        "org_id": world["a"], "item_name": item_name, "spec": "标准件", "unit": "个", "quantity": 10,
        "estimated_price": 100, "reason": "P2397"}).json()["id"]
    assert client.post(f"/api/materials/purchases/{pid}/approve", headers=world["p2397_dir_a"],
                       json={"approved": True}).status_code == 200
    assert client.post(f"/api/materials/purchases/{pid}/contract", headers=world["p2397_op_a"], json={
        "supplier_id": world["supplier"], "contract_no": f"HT-P2397-{pid}", "contract_amount": 1000}).status_code == 200
    return pid


def _asset(client, head, org, code, name):
    got = client.post("/api/mgmt/assets", headers=head, json={"org_id": org, "code": code, "name": name, "quantity": 1})
    assert got.status_code == 201, got.text
    return got.json()["id"]


def _assets(client, admin, org):
    return {a["id"]: a for a in client.get(f"/api/mgmt/assets?org_id={org}", headers=admin).json()}


def test_别家机构先建了同编码的物资_到货照样记在本院(client, admin, world):
    pid = _contracted(client, world, "输液泵")
    squatter = _asset(client, world["p2397_op_b"], world["b"], f"MP{pid:06d}", "乙院的输液泵")
    got = client.post(f"/api/materials/purchases/{pid}/receive", headers=world["p2397_op_a"],
                      json={"received_quantity": 10})
    assert got.status_code == 200, got.text
    assert got.json()["asset_id"] != squatter   # 修前：回执里是乙院那件的物资号
    assert _assets(client, admin, world["b"])[squatter]["quantity"] == 1   # 修前 11：加到了乙院账上
    mine = _assets(client, admin, world["a"])[got.json()["asset_id"]]
    assert (mine["code"], mine["quantity"], mine["name"]) == (f"MP{pid:06d}-2", 10, "输液泵")


def test_本院手工建过同编码的物资_到货另建一件_不混进那件(client, admin, world):
    pid = _contracted(client, world, "治疗车")
    manual = _asset(client, world["p2397_op_a"], world["a"], f"MP{pid:06d}", "办公桌")
    got = client.post(f"/api/materials/purchases/{pid}/receive", headers=world["p2397_op_a"],
                      json={"received_quantity": 10})
    assert got.status_code == 200, got.text
    assets = _assets(client, admin, world["a"])
    assert assets[manual]["quantity"] == 1   # 修前 11：十台治疗车记成了十一张办公桌
    assert (assets[got.json()["asset_id"]]["name"], got.json()["asset_quantity"]) == ("治疗车", 10)


def test_编码没被占用时照旧用单号编码(client, admin, world):
    pid = _contracted(client, world, "监护仪")
    got = client.post(f"/api/materials/purchases/{pid}/receive", headers=world["p2397_op_a"],
                      json={"received_quantity": 10})
    assert got.status_code == 200, got.text
    assert _assets(client, admin, world["a"])[got.json()["asset_id"]]["code"] == f"MP{pid:06d}"
