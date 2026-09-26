"""知识检索先在库里滤掉过期条目、再取最新 200 条（P2-176）。

检索写「过期条目（有效期管理）默认不返回」；实现先按编号倒序取最新 200 条、再在内存里把过期的滤掉——最新那一批里
过期的一多（成批导入的旧政策文件），更早录入、仍在有效期内的条目整个检索不到，页面上一条也搜不出来。
"""
import pytest


@pytest.fixture(scope="module")
def seeded(client, admin):
    from app.database import SessionLocal
    from app.models import KnowledgeEntry, User

    with SessionLocal() as db:
        operator = db.query(User).filter(User.username == "admin").one().id
        valid = KnowledgeEntry(category="drug_policy", title="P2176 现行医保目录说明", expire_date="2099-12-31",
                               created_by=operator)
        db.add(valid)
        db.flush()
        db.add_all([KnowledgeEntry(category="drug_policy", title=f"P2176 旧政策{i}", expire_date="2020-01-01",
                                   created_by=operator) for i in range(200)])   # 之后成批导入 200 份已过期的
        db.commit()
        return {"valid": valid.id}


def test_最新200条都过期_更早的有效条目照样检索得到(client, admin, seeded):
    rows = client.get("/api/knowledge?q=P2176&today=2026-09-26", headers=admin).json()
    assert [r["id"] for r in rows] == [seeded["valid"]]   # 修前 []


def test_显式包含过期的照旧返回并标记(client, admin, seeded):
    rows = client.get("/api/knowledge?q=P2176&include_expired=true&today=2026-09-26", headers=admin).json()
    assert len(rows) == 200 and all(r["expired"] for r in rows)   # 仍是最新 200 条，全是过期的、都标着
