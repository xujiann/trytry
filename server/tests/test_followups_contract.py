"""统一随访中心 `/api/followups` 全部 6 个端点的**特征化网 + 响应契约**。

套路同 test_billing_contract.py / test_maternal_contract.py：先钉住**当前**
响应的完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §7/§11）。

本簇的建模判断（都以此处的精确断言为依据）：

- 本簇**没有 Money/Float 列**；`completion_rate_pct` 恒 float（真除法与兜底
  字面量 `0.0` 两条产地都是浮点——已取消类分母为 0 的行单独钉住 0.0）。
- 任务回执与列表行/超期行**同形**（`_out` 唯一产地，14 键），一个模型；
  `completed_at` 未完成时是**空串**不是 null（`isoformat() if ... else ""`），
  故声明 str 而非 str | None。
- 完成/取消回执只有 id+status 两键，另建模型，不与 14 键行互相注入。
- 统计行 7 键（overdue 在 completion_rate_pct 之前——后者是循环后补进 dict 的，
  键序照 handler 实际出键排）。
- 列表走 `deps.paginate`：limit/offset + X-Total-Count 头照现状钉住；
  排序 due_date 升序、同日按 id。
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

TASK_KEYS = [
    "id", "patient_id", "patient_name", "org_id", "org_name", "category", "category_name",
    "source_id", "title", "due_date", "assigned_to", "status", "result", "completed_at",
]
RECEIPT_KEYS = ["id", "status"]
STAT_ROW_KEYS = [
    "category", "category_name", "pending", "done", "cancelled", "overdue",
    "completion_rate_pct",
]


def d(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


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
    """五条任务铺四类别两机构：fu1 慢病(超期→完成)、fu2 术后(在期待随访)、
    fu3 妇幼(超期→取消)、fu4 出院(超期待随访)、fu5 慢病(乙机构在期待随访)。
    /overdue 的快照在完成/取消**之前**取（那时超期 3 条），之后只剩 fu4。"""
    data: dict = {}
    org = client.post(
        "/api/organizations",
        json={"name": "契约随访医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    org_b = client.post(
        "/api/organizations",
        json={"name": "契约随访卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    data["org"], data["org_b"] = org, org_b
    for username, role in [("fuct_doc", "doctor"), ("fuct_ph", "public_health")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
    data["doctor"] = login(client, "fuct_doc", "pass123456")
    data["ph"] = login(client, "fuct_ph", "pass123456")
    data["patients"] = [
        client.post(
            "/api/patients",
            json={"name": f"契约随访患者{i}", "id_card": f"33088119900101{9501 + i:04d}"},
            headers=admin,
        ).json()
        for i in range(4)
    ]

    def create(payload, headers):
        resp = client.post("/api/followups", json=payload, headers=headers)
        assert resp.status_code == 201, resp.text
        return resp.json()

    data["fu1"] = create({"patient_id": data["patients"][0]["id"], "org_id": org["id"],
                          "category": "chronic", "due_date": d(-3)}, data["doctor"])
    data["fu2"] = create({"patient_id": data["patients"][1]["id"], "org_id": org["id"],
                          "category": "surgery", "source_id": 77, "title": "术后第7天随访",
                          "due_date": d(7), "assigned_to": "王护士"}, data["ph"])
    data["fu3"] = create({"patient_id": data["patients"][2]["id"], "org_id": org["id"],
                          "category": "maternal", "due_date": d(-1)}, data["doctor"])
    data["fu4"] = create({"patient_id": data["patients"][0]["id"], "org_id": org["id"],
                          "category": "discharge", "due_date": d(-5)}, data["ph"])
    data["fu5"] = create({"patient_id": data["patients"][3]["id"], "org_id": org_b["id"],
                          "category": "chronic", "due_date": d(1)}, admin)
    data["overdue_before"] = client.get("/api/followups/overdue", headers=admin).json()
    data["fu1_done"] = client.post(
        f"/api/followups/{data['fu1']['id']}/complete",
        json={"result": "血压 130/80，用药依从性好"}, headers=data["doctor"],
    ).json()
    data["fu3_cancelled"] = client.post(
        f"/api/followups/{data['fu3']['id']}/cancel", headers=data["ph"]
    ).json()
    return data


def test_补建回执精确_标题按类别回落(seed):
    body = seed["fu1"]
    assert list(body.keys()) == TASK_KEYS
    assert body == {
        "id": body["id"],
        "patient_id": seed["patients"][0]["id"],
        "patient_name": "契约随访患者0",
        "org_id": seed["org"]["id"],
        "org_name": "契约随访医院",
        "category": "chronic",
        "category_name": "慢病随访",
        "source_id": 0,
        "title": "慢病随访",  # 未传标题：按类别回落
        "due_date": d(-3),
        "assigned_to": "",
        "status": "pending",
        "result": "",
        "completed_at": "",  # 未完成是空串不是 null
    }
    assert seed["fu2"] == {
        "id": seed["fu2"]["id"],
        "patient_id": seed["patients"][1]["id"],
        "patient_name": "契约随访患者1",
        "org_id": seed["org"]["id"],
        "org_name": "契约随访医院",
        "category": "surgery",
        "category_name": "术后随访",
        "source_id": 77,
        "title": "术后第7天随访",
        "due_date": d(7),
        "assigned_to": "王护士",
        "status": "pending",
        "result": "",
        "completed_at": "",
    }
    assert type(body["source_id"]) is int


def test_完成与取消回执精确_两键封口(seed):
    assert list(seed["fu1_done"].keys()) == RECEIPT_KEYS
    assert seed["fu1_done"] == {"id": seed["fu1"]["id"], "status": "done"}
    assert seed["fu3_cancelled"] == {"id": seed["fu3"]["id"], "status": "cancelled"}


def test_任务列表与回执同形_分页与过滤(client, admin, seed):
    resp = client.get("/api/followups", headers=admin)
    rows = resp.json()
    assert resp.headers["X-Total-Count"] == "5"
    assert [list(r.keys()) for r in rows] == [TASK_KEYS] * 5
    fu1_row = {**seed["fu1"], "status": "done", "result": "血压 130/80，用药依从性好",
               "completed_at": rows[1]["completed_at"]}
    fu3_row = {**seed["fu3"], "status": "cancelled"}
    # due_date 升序：t-5, t-3, t-1, t+1, t+7
    assert rows == [seed["fu4"], fu1_row, fu3_row, seed["fu5"], seed["fu2"]]
    assert isinstance(fu1_row["completed_at"], str) and fu1_row["completed_at"] != ""
    assert client.get(
        f"/api/followups?category=surgery&status=pending&org_id={seed['org']['id']}"
        f"&patient_id={seed['patients'][1]['id']}",
        headers=admin,
    ).json() == [seed["fu2"]]
    assert client.get(
        f"/api/followups?org_id={seed['org_b']['id']}", headers=admin
    ).json() == [seed["fu5"]]
    paged = client.get("/api/followups?offset=1&limit=2", headers=admin)
    assert paged.headers["X-Total-Count"] == "5"
    assert paged.json() == [fu1_row, fu3_row]


def test_超期清单与列表行同形(client, admin, seed):
    # 完成/取消前的快照：三条超期，due_date 升序
    rows = seed["overdue_before"]
    assert [list(r.keys()) for r in rows] == [TASK_KEYS] * 3
    assert rows == [seed["fu4"], seed["fu1"], seed["fu3"]]
    # 完成/取消后只剩出院随访一条超期
    assert client.get("/api/followups/overdue", headers=admin).json() == [seed["fu4"]]
    assert client.get(f"/api/followups/overdue?today={d(-30)}", headers=admin).json() == []


def test_随访统计精确_类别行与完成率(client, admin, seed):
    rows = client.get("/api/followups/stats", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [STAT_ROW_KEYS] * 4  # category 升序
    assert rows == [
        {"category": "chronic", "category_name": "慢病随访", "pending": 1, "done": 1,
         "cancelled": 0, "overdue": 0, "completion_rate_pct": 50.0},
        {"category": "discharge", "category_name": "出院随访", "pending": 1, "done": 0,
         "cancelled": 0, "overdue": 1, "completion_rate_pct": 0.0},
        # 全取消：分母 0 走兜底字面量 0.0（不是 int 0）
        {"category": "maternal", "category_name": "妇幼访视", "pending": 0, "done": 0,
         "cancelled": 1, "overdue": 0, "completion_rate_pct": 0.0},
        {"category": "surgery", "category_name": "术后随访", "pending": 1, "done": 0,
         "cancelled": 0, "overdue": 0, "completion_rate_pct": 0.0},
    ]
    assert isinstance(rows[0]["completion_rate_pct"], float)
    assert isinstance(rows[2]["completion_rate_pct"], float)
    assert type(rows[0]["pending"]) is int and type(rows[1]["overdue"]) is int


def test_各类错误体都只有detail(client, admin, seed):
    cases = [
        client.post("/api/followups",
                    json={"patient_id": 999999, "org_id": seed["org"]["id"],
                          "category": "chronic", "due_date": d(0)},
                    headers=seed["doctor"]),  # 患者不存在 404
        client.post("/api/followups",
                    json={"patient_id": seed["patients"][0]["id"], "org_id": 999999,
                          "category": "chronic", "due_date": d(0)},
                    headers=admin),  # 机构不存在 404
        client.post("/api/followups/999999/complete",
                    json={"result": "无此任务"}, headers=seed["doctor"]),  # 404
        client.post(f"/api/followups/{seed['fu1']['id']}/complete",
                    json={"result": "重复完成"}, headers=seed["doctor"]),  # 已完成 409
        client.post("/api/followups/999999/cancel", headers=seed["ph"]),  # 404
        client.post(f"/api/followups/{seed['fu3']['id']}/cancel",
                    headers=seed["ph"]),  # 已取消 409
        client.get("/api/followups/overdue?today=2026-13-01", headers=admin),  # 日期格式 422
    ]
    assert [r.status_code for r in cases] == [404, 404, 404, 409, 404, 409, 422]
    for r in cases:
        assert set(r.json()) == {"detail"}
