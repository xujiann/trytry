"""预约诊疗 `/api/appointments` 唯一待治理端点（便捷寻医）的**特征化网 + 响应契约**。

套路同 test_maternal_contract.py / test_users_contract.py：先钉住**当前**响应的
完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。其余 10 个端点已治理（SlotOut/AppointmentOut/黑名单三模型），
仅作种子。

本簇的建模判断（都以此处的精确断言为依据）：

- 医师行十键恒在，`next_slots` 是嵌套五键行（slot_id/slot_date/slot_time/
  remaining/resource_name），两级各一个模型；无条件键。
- **没号的医师也返回**并以 `bookable=false`、`next_slots=[]` 标注（业务语义，
  见路由 docstring）——约满号源不进 next_slots 也不计 available_slots，
  用"1 容量号源约满"的种子钉住。
- 排序键 `(-available_slots, employee_id)` 由两行相对顺序钉住。
- 本簇无 Money/Float 出参，数值全 int（remaining=capacity-booked）。
- 员工经 `/api/mgmt/employees` 建档（title_level 缺省 "none" 恒 str）。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

DOCTOR_ROW_KEYS = [
    "employee_id", "name", "title", "title_level", "position", "org_id", "org_name",
    "available_slots", "next_slots", "bookable",
]
NEXT_SLOT_KEYS = ["slot_id", "slot_date", "slot_time", "remaining", "resource_name"]


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_寻医_无匹配医师空清单(client, admin):
    assert client.get("/api/appointments/doctors?keyword=不存在的人", headers=admin).json() == []


@pytest.fixture(scope="module")
def seeded(client, admin):
    """王主任两枚号源（一枚 1 容量被约满）；李医师无号源。"""
    org = client.post(
        "/api/organizations",
        json={"name": "寻医契约总院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    wang = client.post(
        "/api/mgmt/employees",
        json={"org_id": org["id"], "name": "王主任", "title": "主任医师", "position": "呼吸科"},
        headers=admin,
    ).json()
    li = client.post(
        "/api/mgmt/employees",
        json={"org_id": org["id"], "name": "李医师", "title": "主治医师", "position": "全科"},
        headers=admin,
    ).json()
    s1 = client.post(
        "/api/appointments/slots",
        json={"org_id": org["id"], "resource_type": "outpatient", "resource_name": "呼吸科门诊",
              "employee_id": wang["id"], "slot_date": "2026-09-07", "slot_time": "08:30",
              "capacity": 3},
        headers=admin,
    ).json()
    s2 = client.post(
        "/api/appointments/slots",
        json={"org_id": org["id"], "resource_type": "outpatient", "resource_name": "呼吸科门诊",
              "employee_id": wang["id"], "slot_date": "2026-09-08", "slot_time": "14:00",
              "capacity": 1},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "契约就诊人", "id_card": "330281199911112226", "gender": "女",
              "birth_date": "1999-11-11"},
        headers=admin,
    ).json()
    booked = client.post(
        "/api/appointments", json={"slot_id": s2["id"], "patient_id": patient["id"]}, headers=admin
    )
    assert booked.status_code == 201, booked.text  # s2 约满：1 容量 1 已约
    return {"org": org, "wang": wang, "li": li, "s1": s1, "s2": s2}


def test_寻医精确_排序与号源嵌套(client, admin, seeded):
    rows = client.get(
        f"/api/appointments/doctors?org_id={seeded['org']['id']}&from_date=2026-09-01",
        headers=admin,
    ).json()
    assert [list(r.keys()) for r in rows] == [DOCTOR_ROW_KEYS] * 2
    assert [list(s.keys()) for s in rows[0]["next_slots"]] == [NEXT_SLOT_KEYS]
    assert rows == [
        {
            "employee_id": seeded["wang"]["id"],
            "name": "王主任",
            "title": "主任医师",
            "title_level": "none",  # mgmt 建档不设职称等级，缺省恒 "none"
            "position": "呼吸科",
            "org_id": seeded["org"]["id"],
            "org_name": "寻医契约总院",
            "available_slots": 1,  # 约满的 s2 不算余号
            "next_slots": [
                {"slot_id": seeded["s1"]["id"], "slot_date": "2026-09-07", "slot_time": "08:30",
                 "remaining": 3, "resource_name": "呼吸科门诊"},
            ],
            "bookable": True,
        },
        {
            # 没号的也返回并标注——只给有号的，居民会以为这位医师不存在
            "employee_id": seeded["li"]["id"],
            "name": "李医师",
            "title": "主治医师",
            "title_level": "none",
            "position": "全科",
            "org_id": seeded["org"]["id"],
            "org_name": "寻医契约总院",
            "available_slots": 0,
            "next_slots": [],
            "bookable": False,
        },
    ]  # 排序 (-available_slots, employee_id)：有号的王主任在前
    assert type(rows[0]["next_slots"][0]["remaining"]) is int
    assert type(rows[0]["available_slots"]) is int


def test_寻医_关键字过滤同形(client, admin, seeded):
    rows = client.get("/api/appointments/doctors?keyword=王&from_date=2026-09-01", headers=admin).json()
    assert len(rows) == 1 and rows[0]["employee_id"] == seeded["wang"]["id"]
    assert list(rows[0].keys()) == DOCTOR_ROW_KEYS
    # 职称关键字也能命中（title like）
    by_title = client.get(
        "/api/appointments/doctors?keyword=主治&from_date=2026-09-01", headers=admin
    ).json()
    assert [r["employee_id"] for r in by_title] == [seeded["li"]["id"]]
    assert client.get(
        "/api/appointments/doctors?from_date=bad", headers=admin
    ).status_code == 422
