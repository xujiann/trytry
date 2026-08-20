"""转诊状态文案的权威收归后端。

背景：同一份 status→中文 的映射，此前在**前端各存一份**：
`static/core.js` 的 `REF_STATUS` 与 `static/m/m.js` 的 `REFERRAL_STATUS`。
居民端那份上一轮已经删掉、改用后端 `status_label`；业务端这份是最后一处。

注意**不是**要求两个界面用同一套词：居民端说"待接收"（面向患者），
业务端说"待接诊"（面向医师），同一个状态、两个读者、两套措辞是对的。
不对的是同一套措辞在前后端各存一份——那种复制迟早改一处漏一处。
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app
from app.routers.referrals import STATUS_LABELS

CORE_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "core.js"


@pytest.fixture()
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def _admin(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_三个端点都返回status_label(client):
    """创建 / 列表 / 状态流转都要带上——少一个前端就得自己补一张表。"""
    admin = _admin(client)
    orgs = []
    for tag in ("甲", "乙"):
        orgs.append(client.post(
            "/api/organizations",
            json={"name": f"{tag}标签院", "org_type": "lead_hospital", "level": "county"},
            headers=admin,
        ).json())
    client.post(
        "/api/users",
        json={"username": "lbl_doc", "password": "pass123456", "role": "doctor",
              "org_id": orgs[0]["id"]},
        headers=admin,
    )
    doc = client.post(
        "/api/auth/login", json={"username": "lbl_doc", "password": "pass123456"}
    ).json()
    doc = {"Authorization": f"Bearer {doc['access_token']}"}
    patient = client.post(
        "/api/patients", json={"name": "标签患者", "id_card": "330281199101016006"},
        headers=admin,
    ).json()

    created = client.post(
        "/api/referrals",
        json={"patient_id": patient["id"], "from_org_id": orgs[0]["id"],
              "to_org_id": orgs[1]["id"], "direction": "up", "reason": "上转"},
        headers=doc,
    )
    assert created.status_code == 201, created.text
    assert created.json()["status_label"] == "待接诊"

    listed = client.get("/api/referrals", headers=doc).json()
    assert listed[0]["status_label"] == "待接诊"

    moved = client.patch(
        f"/api/referrals/{created.json()['id']}/status",
        json={"status": "accepted"}, headers=doc,
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["status_label"] == "已接诊"


def test_业务端与居民端措辞刻意不同():
    """两个读者、两套措辞——但各自只有一处定义。"""
    from app.routers.portal import _PLATFORM_REFERRAL_STATUS

    assert STATUS_LABELS["pending"] == "待接诊"          # 面向医师
    assert _PLATFORM_REFERRAL_STATUS["pending"] == "待接收"  # 面向患者
    assert set(STATUS_LABELS) == set(_PLATFORM_REFERRAL_STATUS), \
        "两套映射覆盖的状态码必须一致，否则某个状态会在一端显示原始码"


def test_业务端前端不再自带文案表():
    source = CORE_JS.read_text(encoding="utf-8")
    assert "REF_STATUS = {" not in source, "REF_STATUS 文案表应已删除，改用后端 status_label"
    assert "r.status_label" in source, "应从后端取文案"
    # 配色留在前端是对的：配色是展示，文案是口径
    assert "REF_STATUS_COLOR" in source


def test_后端映射覆盖全部可达状态():
    """漏一个状态，界面上就会露出英文原始码。"""
    from app.routers.referrals import _ALLOWED_TRANSITIONS

    reachable = set(_ALLOWED_TRANSITIONS) | {
        s for targets in _ALLOWED_TRANSITIONS.values() for s in targets
    }
    missing = reachable - set(STATUS_LABELS)
    assert not missing, f"这些状态没有中文文案：{missing}"
