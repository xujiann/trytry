"""待办中心 `GET /api/todos` 的**特征化网 + 响应契约**。

套路同 test_rules_contract.py / test_admin_mgmt_contract.py：先钉住**当前**响应的
完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。

本簇的建模判断：

- 顶层 `{role, total, items}` 与分节 `{type, title, count, list}` 都是固定形状，
  逐字段建模；但分节的 `list` 行随 `type` 换形（审方 3 键 / 待诊断 4 键 /
  缺药 4 键 / 危急值 4 键 / 待确认 3 键）——这是**真多态**不是条件键：
  逐字段并模会把五种行的键互相注入 null，而 `critical_ack` 行（id/request_id/
  conclusion）还是 `critical_report` 行的真子集，smart union 会静默吞掉
  `critical_status`。照 metrics/drilldown 的先例用 `list[dict[str, Any]]` 宽字典
  透传，形状由**同一行的 `type` 自描述**，本文件把五种行形各钉一遍。
- 数值全为 int（计数、DrugStock 的 Integer 库存量）；`review_comment` 等空缺省
  是空串不是 null。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

TOP_KEYS = ["role", "total", "items"]
SECTION_KEYS = ["type", "title", "count", "list"]


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def _login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def world(client):
    """一次种全四类待办：待审处方、待诊断申请、缺药、未闭环危急值。"""
    admin = _login(client, "admin", "admin123")
    org = client.post(
        "/api/organizations",
        json={"name": "待办契约医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    for name, role in (("td_doc", "doctor"), ("td_ph", "pharmacist"), ("td_op", "operator")):
        client.post(
            "/api/users",
            json={"username": name, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
    doc = _login(client, "td_doc", "pass123456")
    patient = client.post(
        "/api/patients", json={"name": "待办契约患者", "id_card": "330424199003031234"},
        headers=admin,
    ).json()
    # ① 缺药：quantity < threshold
    stock = client.post(
        "/api/pharmacy/stocks",
        json={"org_id": org["id"], "drug_code": "TD-D1", "drug_name": "待办缺货药",
              "quantity": 3, "threshold": 9},
        headers=admin,
    )
    assert stock.status_code in (200, 201), stock.text
    # ② 待诊断申请（pending）
    pending_exam = client.post(
        "/api/exams",
        json={"patient_id": patient["id"], "from_org_id": org["id"], "center_type": "imaging",
              "item_code": "TD-DR", "item_name": "胸片"},
        headers=doc,
    ).json()
    # ③ 危急值（notified）：另一张申请出报告
    critical_req = client.post(
        "/api/exams",
        json={"patient_id": patient["id"], "from_org_id": org["id"], "center_type": "lab",
              "item_code": "TD-K", "item_name": "血钾"},
        headers=doc,
    ).json()
    report = client.post(
        f"/api/exams/{critical_req['id']}/report",
        json={"conclusion": "血钾 7.0 危急", "critical": True, "reported_by": "检验科"},
        headers=doc,
    ).json()
    # ④ 待药师审：同方重复药品编码必转人工审
    rx = client.post(
        "/api/prescriptions",
        json={"patient_id": patient["id"], "org_id": org["id"], "diagnosis_name": "上感",
              "items": [
                  {"drug_code": "TD-DUP", "drug_name": "重复药", "spec": "10mg",
                   "daily_dose": 1, "days": 3},
                  {"drug_code": "TD-DUP", "drug_name": "重复药", "spec": "10mg",
                   "daily_dose": 1, "days": 3},
              ]},
        headers=doc,
    ).json()
    assert rx["status"] == "pending_review", rx
    return {
        "admin": admin, "doc": doc,
        "ph": _login(client, "td_ph", "pass123456"),
        "op": _login(client, "td_op", "pass123456"),
        "org": org, "pending_exam": pending_exam, "report": report, "rx": rx,
    }


def test_管理员四节精确_五种行形各归其位(client, world):
    body = client.get("/api/todos", headers=world["admin"]).json()
    assert list(body.keys()) == TOP_KEYS
    assert [list(i.keys()) for i in body["items"]] == [SECTION_KEYS] * 4
    assert body == {
        "role": "admin",
        "total": 4,
        "items": [
            {
                "type": "prescription_review",
                "title": "待药师审处方",
                "count": 1,
                "list": [{
                    "id": world["rx"]["id"],
                    "diagnosis_name": "上感",
                    "review_comment": "同方重复药品：重复药（TD-DUP）出现多次，需药师人工审核",
                }],
            },
            {
                "type": "exam_diagnosis",
                "title": "待诊断申请",
                "count": 1,
                "list": [{
                    "id": world["pending_exam"]["id"],
                    "center_type": "imaging",
                    "item_name": "胸片",
                    "status": "pending",
                }],
            },
            {
                "type": "stock_shortage",
                "title": "缺药预警",
                "count": 1,
                "list": [{
                    "org_id": world["org"]["id"],
                    "drug_name": "待办缺货药",
                    "quantity": 3,
                    "threshold": 9,
                }],
            },
            {
                "type": "critical_report",
                "title": "未闭环危急值",
                "count": 1,
                "list": [{
                    "id": world["report"]["id"],
                    "request_id": world["report"]["request_id"],
                    "conclusion": "血钾 7.0 危急",
                    "critical_status": "notified",
                }],
            },
        ],
    }
    # Integer 列与计数全 int（宽字典透传不得把它们变形）
    stock_row = body["items"][2]["list"][0]
    assert type(stock_row["quantity"]) is int and type(stock_row["threshold"]) is int
    assert type(body["total"]) is int


def test_药师视角精确(client, world):
    body = client.get("/api/todos", headers=world["ph"]).json()
    assert body == {
        "role": "pharmacist",
        "total": 1,
        "items": [{
            "type": "prescription_review",
            "title": "待药师审处方",
            "count": 1,
            "list": [{
                "id": world["rx"]["id"],
                "diagnosis_name": "上感",
                "review_comment": "同方重复药品：重复药（TD-DUP）出现多次，需药师人工审核",
            }],
        }],
    }


def test_医师视角精确_待确认行是三键(client, world):
    body = client.get("/api/todos", headers=world["doc"]).json()
    assert body == {
        "role": "doctor",
        "total": 2,
        "items": [
            {
                "type": "exam_diagnosis",
                "title": "待诊断申请",
                "count": 1,
                "list": [{
                    "id": world["pending_exam"]["id"],
                    "center_type": "imaging",
                    "item_name": "胸片",
                    "status": "pending",
                }],
            },
            {
                "type": "critical_ack",
                "title": "待确认危急值",
                "count": 1,
                # 三键行：没有 critical_status——宽字典不得给它注入第四键
                "list": [{
                    "id": world["report"]["id"],
                    "request_id": world["report"]["request_id"],
                    "conclusion": "血钾 7.0 危急",
                }],
            },
        ],
    }
    assert list(body["items"][1]["list"][0].keys()) == ["id", "request_id", "conclusion"]


def test_其他角色空清单精确(client, world):
    assert client.get("/api/todos", headers=world["op"]).json() == {
        "role": "operator", "total": 0, "items": [],
    }
