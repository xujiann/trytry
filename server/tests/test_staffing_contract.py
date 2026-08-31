"""人员调度 `/api/staffing` 五个端点的**特征化网 + 响应契约**。

套路同 test_rules_contract.py / test_admin_mgmt_contract.py：先钉住**当前**响应的
完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。

本簇的建模判断：

- 派驻回执/列表行/结束回执是**同一个 `_out()` 的同一形状**（18 键），一个模型
  三处复用；`days` 是整数天数（int），未结束的按 `date.today()` 算——本文件
  对 ongoing 行用同一天的今天复算，对已结束行用固定日期钉死。
- `dispatch-stats` 的 `caliber` 是**固定两键说明文案**（长期口径/职称口径），
  嵌套模型逐键钉死；`group_id` 是值可空的恒在键（未按分组筛时为 null）→
  `int | None`，无条件键，无需 exclude_unset。
- 全模块无 Money/Float 列出参，数值全为 int。
"""
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

SECONDMENT_KEYS = [
    "id", "employee_id", "employee_name", "title", "title_level", "title_level_name",
    "from_org_id", "from_org_name", "to_org_id", "to_org_name", "start_date", "end_date",
    "ongoing", "assignment_type", "assignment_type_name", "position", "days", "note",
]
TITLE_LEVEL_KEYS = ["id", "name", "title", "title_level", "title_level_name"]
DISPATCH_KEYS = ["year", "group_id", "orgs", "unknown_title_level", "invalid_date_records", "caliber"]
DISPATCH_ORG_KEYS = ["org_id", "org_name", "ongoing", "total", "long_term_6m", "long_term_6m_senior"]


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
def base(client, admin):
    org1 = client.post(
        "/api/organizations",
        json={"name": "调度契约总院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    org2 = client.post(
        "/api/organizations",
        json={"name": "调度契约卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    emp1 = client.post(
        "/api/mgmt/employees",
        json={"org_id": org1["id"], "name": "钱下沉", "title": "主治医师"},
        headers=admin,
    ).json()
    emp2 = client.post(
        "/api/mgmt/employees", json={"org_id": org1["id"], "name": "孙支援"}, headers=admin
    ).json()
    return {"org1": org1, "org2": org2, "emp1": emp1, "emp2": emp2}


@pytest.fixture(scope="module")
def seeded(client, admin, base):
    """emp1：2025 整年长期派驻（已结束，职称中级）→ 2025 年监测口径正好命中；
    emp2：2025 短期支援（已结束，未填等级）+ 2026 长期在派（ongoing 分支）。"""
    client.patch(
        f"/api/staffing/employees/{base['emp1']['id']}/title-level",
        json={"title_level": "intermediate"}, headers=admin,
    )
    s1 = client.post(
        "/api/staffing/secondments",
        json={"employee_id": base["emp1"]["id"], "from_org_id": base["org1"]["id"],
              "to_org_id": base["org2"]["id"], "start_date": "2025-01-01",
              "end_date": "2025-12-31", "assignment_type": "long_term",
              "position": "驻点医师", "note": "年度下沉"},
        headers=admin,
    )
    assert s1.status_code == 201, s1.text
    s2 = client.post(
        "/api/staffing/secondments",
        json={"employee_id": base["emp2"]["id"], "from_org_id": base["org1"]["id"],
              "to_org_id": base["org2"]["id"], "start_date": "2025-03-01",
              "end_date": "2025-06-01", "assignment_type": "support"},
        headers=admin,
    )
    assert s2.status_code == 201, s2.text
    ongoing = client.post(
        "/api/staffing/secondments",
        json={"employee_id": base["emp2"]["id"], "from_org_id": base["org1"]["id"],
              "to_org_id": base["org2"]["id"], "start_date": "2026-01-01",
              "assignment_type": "long_term"},
        headers=admin,
    )
    assert ongoing.status_code == 201, ongoing.text
    return {"s1": s1.json(), "s2": s2.json(), "ongoing": ongoing.json()}


def test_派驻回执精确形状与键序(base, seeded):
    body = seeded["s1"]
    assert list(body.keys()) == SECONDMENT_KEYS
    assert body == {
        "id": body["id"],
        "employee_id": base["emp1"]["id"],
        "employee_name": "钱下沉",
        "title": "主治医师",
        "title_level": "intermediate",
        "title_level_name": "中级",
        "from_org_id": base["org1"]["id"],
        "from_org_name": "调度契约总院",
        "to_org_id": base["org2"]["id"],
        "to_org_name": "调度契约卫生院",
        "start_date": "2025-01-01",
        "end_date": "2025-12-31",
        "ongoing": False,
        "assignment_type": "long_term",
        "assignment_type_name": "长期派驻",
        "position": "驻点医师",
        "days": 364,
        "note": "年度下沉",
    }
    assert type(body["days"]) is int and body["ongoing"] is False


def test_在派回执_days按今天累计(base, seeded):
    body = seeded["ongoing"]
    expected_days = max((date.today() - datetime.strptime("2026-01-01", "%Y-%m-%d").date()).days, 0)
    assert body == {
        "id": body["id"],
        "employee_id": base["emp2"]["id"],
        "employee_name": "孙支援",
        "title": "",
        "title_level": "none",
        "title_level_name": "未填",
        "from_org_id": base["org1"]["id"],
        "from_org_name": "调度契约总院",
        "to_org_id": base["org2"]["id"],
        "to_org_name": "调度契约卫生院",
        "start_date": "2026-01-01",
        "end_date": "",
        "ongoing": True,
        "assignment_type": "long_term",
        "assignment_type_name": "长期派驻",
        "position": "",
        "days": expected_days,
        "note": "",
    }


def test_台账列表与筛选精确(client, admin, seeded):
    resp = client.get("/api/staffing/secondments", headers=admin)
    rows = resp.json()
    assert resp.headers["x-total-count"] == "3"
    assert [list(r.keys()) for r in rows] == [SECONDMENT_KEYS] * 3
    # id 倒序，且行与回执整 dict 相等（同一 _out 形状，无一键漂移）
    assert rows == [seeded["ongoing"], seeded["s2"], seeded["s1"]]
    assert client.get(
        "/api/staffing/secondments?ongoing=true", headers=admin
    ).json() == [seeded["ongoing"]]
    assert client.get(
        "/api/staffing/secondments?assignment_type=support", headers=admin
    ).json() == [seeded["s2"]]
    assert client.get("/api/staffing/secondments?to_org_id=999999", headers=admin).json() == []


def test_职称等级维护回执精确(client, admin, base, seeded):
    body = client.patch(
        f"/api/staffing/employees/{base['emp2']['id']}/title-level",
        json={"title_level": "junior"}, headers=admin,
    ).json()
    assert list(body.keys()) == TITLE_LEVEL_KEYS
    assert body == {
        "id": base["emp2"]["id"],
        "name": "孙支援",
        "title": "",
        "title_level": "junior",
        "title_level_name": "初级",
    }
    # 还原为未填，保住 dispatch-stats 的 unknown 口径素材
    restored = client.patch(
        f"/api/staffing/employees/{base['emp2']['id']}/title-level",
        json={"title_level": "none"}, headers=admin,
    ).json()
    assert restored == {
        "id": base["emp2"]["id"], "name": "孙支援", "title": "",
        "title_level": "none", "title_level_name": "未填",
    }


def test_下沉统计精确形状与键序(client, admin, base, seeded):
    """2025 年口径全用已结束派驻，逐值可复算：emp1 长期 364 天（≥183，中级）计入
    senior；emp2 短期支援不计长期口径。ongoing 的 2026 派驻整段在年外，不入场。"""
    body = client.get("/api/staffing/dispatch-stats?year=2025", headers=admin).json()
    assert list(body.keys()) == DISPATCH_KEYS
    assert [list(o.keys()) for o in body["orgs"]] == [DISPATCH_ORG_KEYS]
    assert body == {
        "year": 2025,
        "group_id": None,   # 值可空的恒在键：未按分组筛时为 null
        "orgs": [{
            "org_id": base["org2"]["id"],
            "org_name": "调度契约卫生院",
            "ongoing": 0,
            "total": 2,
            "long_term_6m": 1,
            "long_term_6m_senior": 1,
        }],
        "unknown_title_level": 0,
        "invalid_date_records": 0,
        "caliber": {
            "long_term_6m": "派驻类型为长期派驻且当年在派满 183 天的人次"
                            "（跨年派驻只计落在本年度内的天数）",
            "senior": "职称等级为中级/副高/正高；等级未填的不计入，单独报 unknown_title_level",
        },
    }


def test_结束派驻回执精确_员工状态联动(client, admin, base, seeded):
    body = client.post(
        f"/api/staffing/secondments/{seeded['ongoing']['id']}/end?end_date=2026-02-01",
        headers=admin,
    ).json()
    assert list(body.keys()) == SECONDMENT_KEYS
    assert body == {**seeded["ongoing"], "end_date": "2026-02-01", "ongoing": False, "days": 31}
    assert client.get("/api/staffing/secondments?ongoing=true", headers=admin).json() == []
