"""公卫协同 `/api/publichealth` 三个待治理端点（处置留痕/诊间提醒）的**特征化网 + 响应契约**。

套路同 test_maternal_contract.py / test_users_contract.py：先钉住**当前**响应的
完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。events/monitors 五个端点已治理（EventOut/MonitorOut），仅作种子。

本簇的建模判断（都以此处的精确断言为依据）：

- 处置动作回执**只回一个 id 键**，与清单行四键不同形——两个模型，不互相注入。
- 清单行的 `at` 是 handler 里 `created_at.isoformat()` 过的**字符串**（非
  datetime 透传），与 DB 值逐字符回绑钉住字节格式。
- 诊间提醒是 `{patient_id, reminders}` 两键；`reminders` 行恒两键
  `{type, detail}`（慢病超期/高危/生活方式/疫苗禁忌/公卫事件五种来源同形，
  detail 是服务端拼好的句子）——一个行模型。
- 本簇无 Money/Float 出参，数值全 int；无条件键。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import PhEventAction


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
def patient(client, admin):
    return client.post(
        "/api/patients",
        json={"name": "契约居民", "id_card": "330281199506067736", "gender": "女",
              "birth_date": "1995-06-06"},
        headers=admin,
    ).json()


def test_诊间提醒_空分支精确(client, admin, patient):
    """放在最前：还没有任何公卫待办/事件时，空 reminders 才钉得住。"""
    body = client.get(
        f"/api/publichealth/reminders/{patient['id']}?today=2026-08-31", headers=admin
    ).json()
    assert list(body.keys()) == ["patient_id", "reminders"]
    assert body == {"patient_id": patient["id"], "reminders": []}
    assert client.get("/api/publichealth/reminders/999999", headers=admin).status_code == 404


@pytest.fixture(scope="module")
def event_actions(client, admin):
    event = client.post(
        "/api/publichealth/events",
        json={"title": "诺如疫情", "level": "III", "disease_name": "诺如病毒",
              "description": "某校聚集性疫情"},
        headers=admin,
    ).json()
    a1 = client.post(
        f"/api/publichealth/events/{event['id']}/actions",
        json={"action": "启动流调", "actor": "王队"},
        headers=admin,
    )
    assert a1.status_code == 201, a1.text
    a2 = client.post(
        f"/api/publichealth/events/{event['id']}/actions", json={"action": "调拨物资"}, headers=admin
    )
    assert a2.status_code == 201, a2.text
    return {"event": event, "a1": a1.json(), "a2": a2.json()}


def test_处置动作回执_只有一个id键(event_actions):
    assert list(event_actions["a1"].keys()) == ["id"]
    assert event_actions["a1"] == {"id": event_actions["a1"]["id"]}
    assert type(event_actions["a1"]["id"]) is int
    assert event_actions["a2"] == {"id": event_actions["a2"]["id"]}


def test_处置留痕清单精确_at回绑DB(client, admin, event_actions):
    rows = client.get(
        f"/api/publichealth/events/{event_actions['event']['id']}/actions", headers=admin
    ).json()
    assert [list(r.keys()) for r in rows] == [["id", "action", "actor", "at"]] * 2
    with SessionLocal() as db:
        iso = {
            a.id: a.created_at.isoformat()
            for a in db.query(PhEventAction).order_by(PhEventAction.id).all()
        }
    assert rows == [
        {"id": event_actions["a1"]["id"], "action": "启动流调", "actor": "王队",
         "at": iso[event_actions["a1"]["id"]]},
        {"id": event_actions["a2"]["id"], "action": "调拨物资", "actor": "",
         "at": iso[event_actions["a2"]["id"]]},
    ]  # id 正序；actor 缺省空串
    assert client.get("/api/publichealth/events/999999/actions", headers=admin).status_code == 404


def test_处置动作_已结案409(client, admin):
    event = client.post(
        "/api/publichealth/events", json={"title": "已结案演练", "level": "IV"}, headers=admin
    ).json()
    closed = client.post(f"/api/publichealth/events/{event['id']}/close", headers=admin)
    assert closed.status_code == 200, closed.text
    resp = client.post(
        f"/api/publichealth/events/{event['id']}/actions", json={"action": "迟到的处置"}, headers=admin
    )
    assert resp.status_code == 409
    assert resp.json() == {"detail": "事件已结案"}


def test_诊间提醒聚合精确(client, admin, patient, event_actions):
    """疫苗禁忌 + 处置中的公卫事件两种来源，行形状同为 {type, detail}。"""
    contra = client.post(
        "/api/vaccination/contraindications",
        json={"patient_id": patient["id"], "vaccine_code": "HPV9", "reason": "急性发热",
              "contra_type": "temporary", "valid_until": "2026-09-15"},
        headers=admin,
    )
    assert contra.status_code == 201, contra.text
    body = client.get(
        f"/api/publichealth/reminders/{patient['id']}?today=2026-08-31", headers=admin
    ).json()
    assert list(body.keys()) == ["patient_id", "reminders"]
    assert [list(r.keys()) for r in body["reminders"]] == [["type", "detail"]] * 2
    assert body == {
        "patient_id": patient["id"],
        "reminders": [
            {"type": "vaccine_contraindication", "detail": "疫苗 HPV9 禁忌：急性发热"},
            {"type": "active_ph_event", "detail": "当前有 1 起突发公卫事件处置中，注意相关症状问诊"},
        ],
    }
