"""智慧急救 `/api/emergency` 唯一待治理端点（绿道时间轴）的**特征化网 + 响应契约**。

套路同 test_maternal_contract.py / test_users_contract.py：先钉住**当前**响应的
完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。其余 7 个端点已治理（CaseOut/MilestoneOut/VitalOut），仅作种子。

本簇的建模判断（都以此处的精确断言为依据）：

- 时间轴节点行四键恒在（milestone/name/occurred_at/recorded）：`occurred_at`
  是**键恒在值可空**——未记录为 null、记录后是录入的 `String(32)` 原文
  （"2026-08-30 09:10" 这种带空格的 ISO 变体，非 isoformat 的 T 分隔）→
  声明 `str | None`，不是条件键，无需 exclude_unset；两种取值各钉一遍。
- 外层五键恒在（case_id/channel_type/status/timeline/recorded_count），
  timeline 恒为 6 行固定节点序列（MILESTONE_SEQUENCE 顺序即行序）。
- 本簇无 Money/Float 出参，数值全 int。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

TIMELINE_KEYS = ["case_id", "channel_type", "status", "timeline", "recorded_count"]
NODE_KEYS = ["milestone", "name", "occurred_at", "recorded"]


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
def case(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "急救契约总院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    created = client.post(
        "/api/emergency/cases",
        json={"location": "县中学操场", "symptom": "胸痛", "ambulance_no": "浙B120",
              "dest_org_id": org["id"], "channel_type": "chest_pain"},
        headers=admin,
    )
    assert created.status_code == 201, created.text
    return created.json()


def test_时间轴_零节点分支精确(client, admin, case):
    body = client.get(f"/api/emergency/cases/{case['id']}/timeline", headers=admin).json()
    assert list(body.keys()) == TIMELINE_KEYS
    assert [list(n.keys()) for n in body["timeline"]] == [NODE_KEYS] * 6
    assert body == {
        "case_id": case["id"],
        "channel_type": "chest_pain",
        "status": "dispatched",
        "timeline": [
            {"milestone": "onset", "name": "发病", "occurred_at": None, "recorded": False},
            {"milestone": "call", "name": "呼救", "occurred_at": None, "recorded": False},
            {"milestone": "depart", "name": "出车", "occurred_at": None, "recorded": False},
            {"milestone": "arrive_scene", "name": "到达现场", "occurred_at": None, "recorded": False},
            {"milestone": "arrive_hospital", "name": "到达医院", "occurred_at": None, "recorded": False},
            {"milestone": "treatment", "name": "开始救治", "occurred_at": None, "recorded": False},
        ],
        "recorded_count": 0,
    }
    assert type(body["recorded_count"]) is int


def test_时间轴_已记录节点回显原文精确(client, admin, case):
    for milestone, at in (("onset", "2026-08-30 09:10"), ("call", "2026-08-30 09:18")):
        resp = client.post(
            f"/api/emergency/cases/{case['id']}/milestones",
            json={"milestone": milestone, "occurred_at": at},
            headers=admin,
        )
        assert resp.status_code == 201, resp.text
    body = client.get(f"/api/emergency/cases/{case['id']}/timeline", headers=admin).json()
    assert list(body.keys()) == TIMELINE_KEYS
    assert body == {
        "case_id": case["id"],
        "channel_type": "chest_pain",
        "status": "dispatched",
        "timeline": [
            # occurred_at 回显录入原文（带空格的 String(32)），不是 isoformat 的 T 分隔
            {"milestone": "onset", "name": "发病", "occurred_at": "2026-08-30 09:10", "recorded": True},
            {"milestone": "call", "name": "呼救", "occurred_at": "2026-08-30 09:18", "recorded": True},
            {"milestone": "depart", "name": "出车", "occurred_at": None, "recorded": False},
            {"milestone": "arrive_scene", "name": "到达现场", "occurred_at": None, "recorded": False},
            {"milestone": "arrive_hospital", "name": "到达医院", "occurred_at": None, "recorded": False},
            {"milestone": "treatment", "name": "开始救治", "occurred_at": None, "recorded": False},
        ],
        "recorded_count": 2,
    }


def test_时间轴_404分支(client, admin):
    resp = client.get("/api/emergency/cases/999999/timeline", headers=admin)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "急救事件不存在"}
