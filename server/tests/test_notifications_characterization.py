"""特征化测试——保护 notifications 四端点从裸 dict 迁移到 response_model。

响应字节必须不变（CLAUDE.md 第7条）。本测试钉住迁移前每端点的精确键集合，
迁移后仍须全绿。配方见 docs/接口标准与治理.md。

注意：notification_out 被 portal 复用，迁移只给 notifications 端点加 response_model，
不动该函数——portal 的 /me/notifications 不受影响。
"""
from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models import Notification, User

LIST_KEYS = {"id", "category", "title", "body", "link_type", "link_id", "read", "created_at"}


@pytest.fixture(scope="module")
def seeded(client, admin):
    db = SessionLocal()
    try:
        uid = db.query(User).filter(User.username == "admin").first().id
        n = Notification(
            user_id=uid, category="test", title="标题", body="正文",
            link_type="exam", link_id=7,
        )
        db.add(n)
        db.commit()
        nid = n.id
    finally:
        db.close()
    return {"nid": nid}


def test_list_键恰好为八个(admin, seeded, client):
    rows = client.get("/api/notifications", headers=admin).json()
    assert rows, "至少应有一条"
    for row in rows:
        assert set(row.keys()) == LIST_KEYS, f"键漂移：{set(row.keys())}"
        assert isinstance(row["read"], bool)


def test_unread_count_键(admin, seeded, client):
    out = client.get("/api/notifications/unread-count", headers=admin).json()
    assert set(out.keys()) == {"unread"} and isinstance(out["unread"], int)


def test_mark_read_键(admin, seeded, client):
    out = client.post(f"/api/notifications/{seeded['nid']}/read", headers=admin).json()
    assert set(out.keys()) == {"id", "read"}
    assert out["id"] == seeded["nid"] and out["read"] is True


def test_read_all_键(admin, seeded, client):
    out = client.post("/api/notifications/read-all", headers=admin).json()
    assert set(out.keys()) == {"marked"} and isinstance(out["marked"], int)
