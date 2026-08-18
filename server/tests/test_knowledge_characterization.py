"""特征化测试——保护 knowledge 四个端点从裸 dict 迁移到 response_model。

    混乱代码（裸 dict）  →  标准接口（response_model）

迁移目标是加 response_model，响应字节必须不变（CLAUDE.md 第7条）。本测试钉住
迁移前每个端点的精确键集合，迁移后仍须全绿。配方见 docs/接口标准与治理.md。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

CREATE_KEYS = {"id", "category", "title"}
UPDATE_KEYS = {"id", "active", "expire_date"}
SEARCH_KEYS = {"id", "category", "category_name", "title", "body", "expire_date", "expired"}
EXPIRING_KEYS = {"id", "category", "title", "expire_date"}


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
        json={"name": "知识库特征化医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    client.post(
        "/api/users",
        json={"username": "kn_dir", "password": "pass123456", "role": "director", "org_id": org["id"]},
        headers=admin,
    )
    dir_h = _login(client, "kn_dir", "pass123456")
    created = client.post(
        "/api/knowledge",
        json={"category": "regulation", "title": "病历质控制度", "body": "正文", "expire_date": "2026-06-15"},
        headers=dir_h,
    ).json()
    return {"dir": dir_h, "created": created}


def test_create_键恰好为三个(ctx):
    assert set(ctx["created"].keys()) == CREATE_KEYS, f"键漂移：{set(ctx['created'].keys())}"


def test_update_键恰好为三个(ctx, client):
    out = client.patch(
        f"/api/knowledge/{ctx['created']['id']}", json={"active": True}, headers=ctx["dir"]
    ).json()
    assert set(out.keys()) == UPDATE_KEYS, f"键漂移：{set(out.keys())}"


def test_search_每行键恰好为七个(ctx, client):
    rows = client.get("/api/knowledge?today=2026-05-01", headers=ctx["dir"]).json()
    assert rows, "至少应有一条在用条目"
    for row in rows:
        assert set(row.keys()) == SEARCH_KEYS, f"键漂移：{set(row.keys())}"
        assert isinstance(row["expired"], bool)


def test_expiring_每行键恰好为四个(ctx, client):
    rows = client.get("/api/knowledge/expiring?days=30&today=2026-06-01", headers=ctx["dir"]).json()
    assert any(r["id"] == ctx["created"]["id"] for r in rows), "临期条目应出现"
    for row in rows:
        assert set(row.keys()) == EXPIRING_KEYS, f"键漂移：{set(row.keys())}"
