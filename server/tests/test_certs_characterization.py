"""特征化测试——保护 certs 三个端点从裸 dict 迁移到 response_model。

    混乱代码（裸 dict，无契约）  →  标准接口（response_model 声明契约）

迁移目标是加 response_model，但**响应字节必须一模一样**（CLAUDE.md 第7条）。
本测试钉住迁移前每个端点的精确键集合与取值——迁移后仍须全绿。
配方见 docs/接口标准与治理.md。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

ISSUE_KEYS = {"id", "cert_type", "cert_type_name", "cert_no", "name", "event_date"}
LIST_KEYS = {"id", "cert_type", "cert_no", "name", "gender", "event_date", "detail", "org_id"}


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
def ctx(client):
    admin = _login(client, "admin", "admin123")
    org = client.post(
        "/api/organizations",
        json={"name": "证明特征化医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    client.post(
        "/api/users",
        json={"username": "cert_doc", "password": "pass123456", "role": "doctor", "org_id": org["id"]},
        headers=admin,
    )
    doc = _login(client, "cert_doc", "pass123456")
    issued = client.post(
        "/api/certs",
        json={"cert_type": "birth", "name": "新生儿甲", "gender": "男",
              "event_date": "2026-06-01", "org_id": org["id"]},
        headers=doc,
    ).json()
    return {"doc": doc, "org": org, "issued": issued}


def test_issue_返回键恰好为六个(ctx):
    assert set(ctx["issued"].keys()) == ISSUE_KEYS, f"键漂移：{set(ctx['issued'].keys())}"
    assert ctx["issued"]["cert_type"] == "birth"
    assert ctx["issued"]["cert_type_name"] == "出生医学证明"
    assert ctx["issued"]["cert_no"].startswith("B")


def test_list_每行键恰好为八个(ctx, client):
    rows = client.get("/api/certs", headers=ctx["doc"]).json()
    assert rows, "至少应有一条"
    for row in rows:
        assert set(row.keys()) == LIST_KEYS, f"键漂移：{set(row.keys())}"


def test_stats_按类型计数(ctx, client):
    stats = client.get("/api/certs/stats", headers=ctx["doc"]).json()
    assert stats.get("birth", 0) >= 1
    assert all(isinstance(v, int) for v in stats.values())
