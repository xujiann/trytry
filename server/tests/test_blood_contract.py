"""血液管理 `/api/blood` 全部 6 个端点的**特征化网 + 响应契约**。

套路同 test_billing_contract.py / test_maternal_contract.py：先钉住**当前**
响应的完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §7/§11）。

本簇的建模判断（都以此处的精确断言为依据）：

- 本簇**没有 Money/Float 列**：`quantity_ml`/`stock_remaining_ml` 都是 Integer
  列（血按毫升整数计），恒 int——声明成 float 会把「600 毫升」印成「600.0」，
  用 `type(x) is int` 显式钉（dict 相等对 600==600.0 是盲的）。
- 四种回执三个形状：库存回执（3 键，插入与累加两条路同形）；申请/审批回执
  （id+status 两键，共用一个模型）；发血回执多一个尾键 `stock_remaining_ml`
  ——键集合不同就分开建模，不互相注入。
- 列表行 7 键（不含 reason/requested_by 等库里有而出参没有的列），
  与回执不同形，单独一个模型。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

STOCK_KEYS = ["blood_type", "component", "quantity_ml"]
RECEIPT_KEYS = ["id", "status"]
ISSUE_KEYS = ["id", "status", "stock_remaining_ml"]
REQUEST_ROW_KEYS = ["id", "patient_id", "org_id", "blood_type", "component", "quantity_ml", "status"]


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def seed(client, admin):
    """一次种完全部场景：A/rbc 两次入库累加 600，O/plasma 300；
    三张申请分别走 发血 / 驳回 / 待审批 三条终线。"""
    data: dict = {}
    org = client.post(
        "/api/organizations",
        json={"name": "契约血库医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    data["org"] = org
    for username, role in [("blood_doc", "doctor"), ("blood_op", "operator"),
                           ("blood_dir", "director")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
    data["doctor"] = login(client, "blood_doc", "pass123456")
    data["operator"] = login(client, "blood_op", "pass123456")
    data["director"] = login(client, "blood_dir", "pass123456")
    data["patients"] = [
        client.post(
            "/api/patients",
            json={"name": f"契约用血患者{i}", "id_card": f"33088119900101{7901 + i:04d}"},
            headers=admin,
        ).json()
        for i in range(3)
    ]

    data["up1"] = client.post(
        "/api/blood/stocks",
        json={"blood_type": "A", "component": "rbc", "quantity_ml": 400},
        headers=data["operator"],
    ).json()
    data["up2"] = client.post(
        "/api/blood/stocks",
        json={"blood_type": "A", "component": "rbc", "quantity_ml": 200},
        headers=data["operator"],
    ).json()
    data["up3"] = client.post(
        "/api/blood/stocks",
        json={"blood_type": "O", "component": "plasma", "quantity_ml": 300},
        headers=data["operator"],
    ).json()

    def request(patient, payload):
        resp = client.post(
            "/api/blood/requests",
            json={"patient_id": patient["id"], "org_id": org["id"], **payload},
            headers=data["doctor"],
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    data["r1"] = request(data["patients"][0],
                         {"blood_type": "A", "component": "rbc", "quantity_ml": 200,
                          "reason": "术中备血"})
    data["r1_approved"] = client.post(
        f"/api/blood/requests/{data['r1']['id']}/review?approve=true", headers=data["director"]
    ).json()
    data["r1_issued"] = client.post(
        f"/api/blood/requests/{data['r1']['id']}/issue", headers=data["operator"]
    ).json()
    data["r2"] = request(data["patients"][1],
                         {"blood_type": "O", "component": "plasma", "quantity_ml": 100})
    data["r2_rejected"] = client.post(
        f"/api/blood/requests/{data['r2']['id']}/review?approve=false", headers=data["director"]
    ).json()
    data["r3"] = request(data["patients"][2],
                         {"blood_type": "B", "component": "platelet", "quantity_ml": 150,
                          "reason": "血小板减少"})
    return data


def test_库存回执精确_插入与累加同形(seed):
    assert list(seed["up1"].keys()) == STOCK_KEYS
    assert seed["up1"] == {"blood_type": "A", "component": "rbc", "quantity_ml": 400}
    # 第二次入库走累加分支，回执同形、数量已累加
    assert seed["up2"] == {"blood_type": "A", "component": "rbc", "quantity_ml": 600}
    assert seed["up3"] == {"blood_type": "O", "component": "plasma", "quantity_ml": 300}
    # Integer 列：声明成 float 会把 600 印成 600.0
    assert type(seed["up2"]["quantity_ml"]) is int


def test_库存列表与回执同形_血型成分排序(client, admin, seed):
    rows = client.get("/api/blood/stocks", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [STOCK_KEYS] * 2
    # 发血 200 已从 A/rbc 扣减（600-200=400）；排序 blood_type, component
    assert rows == [
        {"blood_type": "A", "component": "rbc", "quantity_ml": 400},
        {"blood_type": "O", "component": "plasma", "quantity_ml": 300},
    ]
    assert type(rows[0]["quantity_ml"]) is int


def test_申请与审批回执精确_两键封口(seed):
    assert list(seed["r1"].keys()) == RECEIPT_KEYS
    assert seed["r1"] == {"id": seed["r1"]["id"], "status": "pending"}
    assert seed["r1_approved"] == {"id": seed["r1"]["id"], "status": "approved"}
    assert seed["r2_rejected"] == {"id": seed["r2"]["id"], "status": "rejected"}
    assert type(seed["r1"]["id"]) is int


def test_发血回执精确_多一个库存尾键(seed):
    body = seed["r1_issued"]
    assert list(body.keys()) == ISSUE_KEYS
    assert body == {"id": seed["r1"]["id"], "status": "issued", "stock_remaining_ml": 400}
    assert type(body["stock_remaining_ml"]) is int


def test_申请列表精确_键序与过滤(client, admin, seed):
    rows = client.get("/api/blood/requests", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [REQUEST_ROW_KEYS] * 3
    r3_row = {
        "id": seed["r3"]["id"], "patient_id": seed["patients"][2]["id"],
        "org_id": seed["org"]["id"], "blood_type": "B", "component": "platelet",
        "quantity_ml": 150, "status": "pending",
    }
    r2_row = {
        "id": seed["r2"]["id"], "patient_id": seed["patients"][1]["id"],
        "org_id": seed["org"]["id"], "blood_type": "O", "component": "plasma",
        "quantity_ml": 100, "status": "rejected",
    }
    r1_row = {
        "id": seed["r1"]["id"], "patient_id": seed["patients"][0]["id"],
        "org_id": seed["org"]["id"], "blood_type": "A", "component": "rbc",
        "quantity_ml": 200, "status": "issued",
    }
    assert rows == [r3_row, r2_row, r1_row]  # id 倒序
    assert client.get("/api/blood/requests?status=issued", headers=admin).json() == [r1_row]
    assert client.get(
        f"/api/blood/requests?org_id={seed['org']['id']}&status=pending", headers=admin
    ).json() == [r3_row]
    assert client.get("/api/blood/requests?status=没有这种状态", headers=admin).json() == []


def test_各类错误体都只有detail(client, admin, seed):
    cases = [
        client.post("/api/blood/requests",
                    json={"patient_id": 999999, "org_id": seed["org"]["id"], "blood_type": "A",
                          "component": "rbc", "quantity_ml": 100},
                    headers=seed["doctor"]),  # 患者不存在 404
        client.post("/api/blood/requests/999999/review?approve=true",
                    headers=seed["director"]),  # 申请不存在 404
        client.post(f"/api/blood/requests/{seed['r1']['id']}/review?approve=true",
                    headers=seed["director"]),  # 已处理 409
        client.post("/api/blood/requests/999999/issue", headers=seed["operator"]),  # 404
        client.post(f"/api/blood/requests/{seed['r3']['id']}/issue",
                    headers=seed["operator"]),  # 未审批 409
    ]
    # 库存不足：B/platelet 无库存行，审批后发血应 409 且不改状态
    approved = client.post(
        f"/api/blood/requests/{seed['r3']['id']}/review?approve=true", headers=seed["director"]
    )
    assert approved.status_code == 200
    cases.append(client.post(f"/api/blood/requests/{seed['r3']['id']}/issue",
                             headers=seed["operator"]))  # 库存不足 409
    assert [r.status_code for r in cases] == [404, 404, 409, 404, 409, 409]
    for r in cases:
        assert set(r.json()) == {"detail"}
