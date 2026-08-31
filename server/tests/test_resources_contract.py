"""统一资源与排程撮合 `/api/resources` 八个端点的**特征化网 + 响应契约**。

套路同 `test_rules_contract.py` / `test_dataquality_contract.py`：先钉住**当前**
响应的完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。

建模判断：

- 本模块没有 Money/Float 列出参：`capacity`/`booked`/`quantity_ml` 全是 Integer，
  `available` 是它们的原值或差值，恒 int（或 None）。
- `/catalog` 的五类行形状**完全一致**（同九个键同顺序），不是多态，逐字段建模；
  `org_id` 只在血制品行为 None（血库全县一本账，无 org_id），`available` 在
  检查资源与手术间行恒 None（它们没有"余量"概念）。
- `/match/or-rooms` 是**条件键**双分支：无启用手术间 → `scheduled_date/rooms/hint`；
  正常 → `scheduled_date/window/rooms/caliber`。两条分支的键集合都在此钉死
  （包括"另一半键**整个不在**"），对应端点 `response_model_exclude_unset=True`。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


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


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "资源契约县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def bare_org(client, admin):
    """没有任何手术间的机构：钉 /match/or-rooms 的 hint 分支。"""
    return client.post(
        "/api/organizations",
        json={"name": "资源契约空卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()


# ------------------------------------------------------------- 通用资源登记

RESOURCE_KEY_ORDER = [
    "id", "org_id", "resource_type", "resource_type_name", "code", "name",
    "capacity", "unit", "location", "contact", "status", "status_name",
    "withdraw_reason", "note",
]


@pytest.fixture(scope="module")
def seeded_resources(client, admin, org):
    logistics = client.post(
        "/api/resources",
        json={"org_id": org["id"], "resource_type": "logistics", "code": "CTLG1",
              "name": "标本转运工勤", "capacity": 3, "unit": "人次",
              "location": "后勤楼", "contact": "张三 13800000000", "note": "契约备注"},
        headers=admin,
    )
    assert logistics.status_code == 201, logistics.text
    equipment = client.post(
        "/api/resources",
        json={"org_id": org["id"], "resource_type": "equipment", "code": "CTEQ1",
              "name": "移动DR"},
        headers=admin,
    )
    assert equipment.status_code == 201, equipment.text
    return {"logistics": logistics.json(), "equipment": equipment.json()}


def test_登记回执精确形状与键序(seeded_resources):
    body = seeded_resources["logistics"]
    assert list(body.keys()) == RESOURCE_KEY_ORDER
    assert body == {
        "id": body["id"],
        "org_id": body["org_id"],
        "resource_type": "logistics",
        "resource_type_name": "工勤服务",
        "code": "CTLG1",
        "name": "标本转运工勤",
        "capacity": 3,
        "unit": "人次",
        "location": "后勤楼",
        "contact": "张三 13800000000",
        "status": "draft",
        "status_name": "草稿",
        "withdraw_reason": "",
        "note": "契约备注",
    }
    # 缺省分支：capacity=1、其余空串（不是 null）
    minimal = seeded_resources["equipment"]
    assert minimal == {
        "id": minimal["id"],
        "org_id": minimal["org_id"],
        "resource_type": "equipment",
        "resource_type_name": "通用设备",
        "code": "CTEQ1",
        "name": "移动DR",
        "capacity": 1,
        "unit": "",
        "location": "",
        "contact": "",
        "status": "draft",
        "status_name": "草稿",
        "withdraw_reason": "",
        "note": "",
    }


def test_列表与回执同形(client, admin, seeded_resources):
    rows = client.get("/api/resources", headers=admin).json()
    # id 倒序：后登记的在前
    assert rows == [seeded_resources["equipment"], seeded_resources["logistics"]]
    assert list(rows[0].keys()) == RESOURCE_KEY_ORDER
    only_lg = client.get("/api/resources?resource_type=logistics", headers=admin).json()
    assert only_lg == [seeded_resources["logistics"]]
    assert client.get("/api/resources?status=withdrawn", headers=admin).json() == []


def test_更新回执精确(client, admin, seeded_resources):
    resource = seeded_resources["logistics"]
    body = client.patch(
        f"/api/resources/{resource['id']}",
        json={"capacity": 5, "location": "后勤楼二层"},
        headers=admin,
    ).json()
    assert body == {**resource, "capacity": 5, "location": "后勤楼二层"}


def test_发布与撤回回执精确(client, admin, seeded_resources):
    resource_id = seeded_resources["logistics"]["id"]
    patched = {**seeded_resources["logistics"], "capacity": 5, "location": "后勤楼二层"}
    published = client.post(f"/api/resources/{resource_id}/publish", headers=admin).json()
    assert published == {**patched, "status": "published", "status_name": "已发布"}
    assert list(published.keys()) == RESOURCE_KEY_ORDER
    withdrawn = client.post(
        f"/api/resources/{resource_id}/withdraw", json={"reason": "设备检修"}, headers=admin
    ).json()
    assert withdrawn == {
        **patched, "status": "withdrawn", "status_name": "已撤回", "withdraw_reason": "设备检修",
    }
    # 重新发布：撤回理由清空（不是残留）
    again = client.post(f"/api/resources/{resource_id}/publish", headers=admin).json()
    assert again == {**patched, "status": "published", "status_name": "已发布"}


# ------------------------------------------------------------- 统一资源视图

CATALOG_ITEM_KEY_ORDER = [
    "kind", "kind_name", "id", "org_id", "name", "detail", "available", "unit", "usable",
]
CATALOG_CALIBER = (
    "available 按各类资源自己的规则算，不强行统一成一个数——"
    "号源看余量、检查资源与手术间看启停、血制品看库存、通用资源看发布状态"
)


@pytest.fixture(scope="module")
def domain_resources(client, admin, org, seeded_resources):
    """五类领域资源各一：号源 / 检查资源 / 手术间 / 血制品（+已有的两条通用）。"""
    slot = client.post(
        "/api/appointments/slots",
        json={"org_id": org["id"], "resource_type": "outpatient", "resource_name": "内科门诊",
              "slot_date": "2099-10-01", "slot_time": "09:00", "capacity": 5},
        headers=admin,
    )
    assert slot.status_code == 201, slot.text
    exam = client.post(
        "/api/exams/resources",
        json={"org_id": org["id"], "center_type": "imaging", "item_name": "DR摄影",
              "device": "DR-01", "duration_min": 20},
        headers=admin,
    )
    assert exam.status_code == 201, exam.text
    room = client.post(
        "/api/surgery/rooms", json={"org_id": org["id"], "name": "契约一号手术间"}, headers=admin
    )
    assert room.status_code == 201, room.text
    blood = client.post(
        "/api/blood/stocks",
        json={"blood_type": "A", "component": "rbc", "quantity_ml": 2000},
        headers=admin,
    )
    assert blood.status_code == 200, blood.text
    return {"slot": slot.json(), "exam": exam.json(), "room": room.json()}


def test_统一视图五类精确形状与键序(client, admin, org, seeded_resources, domain_resources):
    body = client.get("/api/resources/catalog", headers=admin).json()
    assert list(body.keys()) == ["total", "by_kind", "items", "caliber"]
    for item in body["items"]:
        assert list(item.keys()) == CATALOG_ITEM_KEY_ORDER
    assert body == {
        "total": 6,
        # 键序 = 五类固定的聚合顺序（slot → exam → or_room → blood → general）
        "by_kind": {
            "slot": {"total": 1, "usable": 1},
            "exam": {"total": 1, "usable": 1},
            "or_room": {"total": 1, "usable": 1},
            "blood": {"total": 1, "usable": 1},
            "general": {"total": 2, "usable": 1},
        },
        "items": [
            {"kind": "slot", "kind_name": "号源", "id": domain_resources["slot"]["id"],
             "org_id": org["id"], "name": "内科门诊", "detail": "2099-10-01 09:00",
             "available": 5, "unit": "个", "usable": True},
            {"kind": "exam", "kind_name": "检查资源", "id": domain_resources["exam"]["id"],
             "org_id": org["id"], "name": "DR摄影", "detail": "DR-01 20分钟",
             "available": None, "unit": "", "usable": True},
            {"kind": "or_room", "kind_name": "手术间", "id": domain_resources["room"]["id"],
             "org_id": org["id"], "name": "契约一号手术间", "detail": "",
             "available": None, "unit": "", "usable": True},
            # 血库全县一本账：org_id 恒 None（不是 0，也不是缺键）
            {"kind": "blood", "kind_name": "血制品", "id": body["items"][3]["id"],
             "org_id": None, "name": "A rbc", "detail": "全县共用血库",
             "available": 2000, "unit": "ml", "usable": True},
            {"kind": "general", "kind_name": "工勤服务",
             "id": seeded_resources["logistics"]["id"], "org_id": org["id"],
             "name": "标本转运工勤", "detail": "后勤楼二层", "available": 5,
             "unit": "人次", "usable": True},
            {"kind": "general", "kind_name": "通用设备",
             "id": seeded_resources["equipment"]["id"], "org_id": org["id"],
             "name": "移动DR", "detail": "", "available": 1, "unit": "", "usable": False},
        ],
        "caliber": CATALOG_CALIBER,
    }


def test_统一视图按机构筛时血库不列出(client, admin, org, seeded_resources, domain_resources):
    """血库无 org_id：按机构筛还把全县血库列出来会误导人——钉住"不出现"。"""
    body = client.get(f"/api/resources/catalog?org_id={org['id']}", headers=admin).json()
    assert body["total"] == 5
    assert "blood" not in body["by_kind"]
    only_general = client.get(
        "/api/resources/catalog?resource_kind=general", headers=admin
    ).json()
    assert only_general["total"] == 2
    assert set(only_general["by_kind"]) == {"general"}


# ------------------------------------------------------------- 排程撮合

SLOT_MATCH_CALIBER = "按最早可用日期排序；不自动选——最早与最近常不是同一家，选哪个是患者的事"


def test_号源撮合精确形状与键序(client, admin, org, domain_resources):
    body = client.get(
        "/api/resources/match/slots",
        params={"from_date": "2099-09-30", "keyword": "内科"},
        headers=admin,
    ).json()
    assert list(body.keys()) == ["window", "candidates", "caliber"]
    assert list(body["candidates"][0].keys()) == [
        "org_id", "org_name", "earliest", "remaining_total", "slots",
    ]
    assert list(body["candidates"][0]["slots"][0].keys()) == [
        "slot_id", "resource_name", "slot_date", "slot_time", "remaining",
    ]
    assert body == {
        "window": {"start": "2099-09-30", "end": "2099-10-13"},
        "candidates": [
            {"org_id": org["id"], "org_name": "资源契约县医院", "earliest": "2099-10-01",
             "remaining_total": 5,
             "slots": [{"slot_id": domain_resources["slot"]["id"], "resource_name": "内科门诊",
                        "slot_date": "2099-10-01", "slot_time": "09:00", "remaining": 5}]},
        ],
        "caliber": SLOT_MATCH_CALIBER,
    }
    # 空命中分支：candidates 为空列表，window/caliber 照常
    empty = client.get(
        "/api/resources/match/slots",
        params={"resource_type": "lab", "from_date": "2099-09-30"},
        headers=admin,
    ).json()
    assert empty == {
        "window": {"start": "2099-09-30", "end": "2099-10-13"},
        "candidates": [],
        "caliber": SLOT_MATCH_CALIBER,
    }


OR_ROOM_CALIBER = (
    "冲突判定与 /api/surgery 排班接口同一套规则；空档为请求窗口内"
    "被已排时段切剩的连续区间"
)


@pytest.fixture(scope="module")
def scheduled_surgery(client, admin, org, domain_resources):
    """在契约一号手术间 2099-10-08 排一台 10:00-12:00 的手术，钉冲突与空档行。"""
    ward = client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "契约外科"}, headers=admin
    ).json()
    bed = client.post(
        "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "CT-1"}, headers=admin
    ).json()
    patient = client.post(
        "/api/patients", json={"name": "资源契约患者", "id_card": "330281199203046014"},
        headers=admin,
    ).json()
    admission = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"],
              "diagnosis_name": "胆囊结石"},
        headers=admin,
    ).json()
    # 审批人不得是申请人：申请以独立医师账号提出，管理员审批
    client.post(
        "/api/users",
        json={"username": "ct_res_doc", "password": "passw0rd1", "role": "doctor",
              "org_id": org["id"]},
        headers=admin,
    )
    doctor = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", json={"username": "ct_res_doc", "password": "passw0rd1"}
    ).json()["access_token"]}
    request = client.post(
        "/api/surgery/requests",
        json={"admission_id": admission["id"], "surgery_name": "胆囊切除术"},
        headers=doctor,
    ).json()
    approve = client.post(
        f"/api/surgery/requests/{request['id']}/approve", json={"approved": True}, headers=admin
    )
    assert approve.status_code == 200, approve.text
    schedule = client.post(
        f"/api/surgery/requests/{request['id']}/schedule",
        json={"room_id": domain_resources["room"]["id"], "scheduled_date": "2099-10-08",
              "start_time": "10:00", "end_time": "12:00"},
        headers=admin,
    )
    assert schedule.status_code in (200, 201), schedule.text
    return request


def test_手术间撮合精确形状与键序(client, admin, org, domain_resources, scheduled_surgery):
    body = client.get(
        "/api/resources/match/or-rooms",
        params={"org_id": org["id"], "scheduled_date": "2099-10-08",
                "start_time": "09:00", "end_time": "13:00"},
        headers=admin,
    ).json()
    # 正常分支四个键——没有 hint（条件键：整个不出现，不是 null）
    assert list(body.keys()) == ["scheduled_date", "window", "rooms", "caliber"]
    assert list(body["rooms"][0].keys()) == [
        "room_id", "room_name", "available", "conflicts", "gaps",
    ]
    assert body == {
        "scheduled_date": "2099-10-08",
        "window": {"start_time": "09:00", "end_time": "13:00"},
        "rooms": [
            {"room_id": domain_resources["room"]["id"], "room_name": "契约一号手术间",
             "available": False,
             "conflicts": [{"start_time": "10:00", "end_time": "12:00"}],
             "gaps": [{"start_time": "09:00", "end_time": "10:00"},
                      {"start_time": "12:00", "end_time": "13:00"}]},
        ],
        "caliber": OR_ROOM_CALIBER,
    }
    # 不相交窗口：无冲突，空档为整个窗口
    later = client.get(
        "/api/resources/match/or-rooms",
        params={"org_id": org["id"], "scheduled_date": "2099-10-08",
                "start_time": "14:00", "end_time": "16:00"},
        headers=admin,
    ).json()
    assert later == {
        "scheduled_date": "2099-10-08",
        "window": {"start_time": "14:00", "end_time": "16:00"},
        "rooms": [
            {"room_id": domain_resources["room"]["id"], "room_name": "契约一号手术间",
             "available": True, "conflicts": [],
             "gaps": [{"start_time": "14:00", "end_time": "16:00"}]},
        ],
        "caliber": OR_ROOM_CALIBER,
    }


def test_手术间撮合无手术间分支精确(client, admin, bare_org):
    body = client.get(
        "/api/resources/match/or-rooms",
        params={"org_id": bare_org["id"], "scheduled_date": "2099-10-08"},
        headers=admin,
    ).json()
    # hint 分支三个键——没有 window/caliber（条件键的另一半：同样整个不出现）
    assert list(body.keys()) == ["scheduled_date", "rooms", "hint"]
    assert body == {
        "scheduled_date": "2099-10-08",
        "rooms": [],
        "hint": "该机构没有启用中的手术间",
    }
