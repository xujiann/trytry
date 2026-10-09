"""知识条目的有效期不得早于今天：续期、发布填了过去的日期都 422（P2-1668，第四十九批扫描 AM4-4）。

`EntryCreate.expire_date` / `EntryUpdate.expire_date` 原先只查格式（P1-61）。扫描实测（修前代码）：续期把 2027-10-20 敲成
2025-10-20，`PATCH /api/knowledge/{id}` 照收 200——默认检索滤掉过期的、临期提醒只列今天及以后到期的，这一条当场从两处同时
消失，页面上只提示成功，只有勾上「含过期」才看得到；新发布时填了过去的日期也一样，201 之后哪儿都不显示。

修法：有效期不得早于今天（`clock.today()` 本地业务日，当天照收），报人话 422、库不动；照慢病「下次随访日不得早于今天」
（P2-1545）。只在请求里带了 `expire_date` 时判：留空 = 长期有效照收；不带它的 PATCH（停用、修订正文）不受影响，存量已过期的
行照旧能停用。
"""
from datetime import date

import pytest

from conftest import freeze_business_date

from app.database import SessionLocal
from app.models import KnowledgeEntry, User

#: 冻住的业务日：判据走 `clock.today()`，冻住它就覆盖了这条校验
TODAY = date(2026, 3, 10)


def _row(entry_id: int) -> tuple[str, bool, str]:
    with SessionLocal() as db:
        e = db.get(KnowledgeEntry, entry_id)
        return e.expire_date, e.active, e.body


def _count(title: str) -> int:
    with SessionLocal() as db:
        return db.query(KnowledgeEntry).filter(KnowledgeEntry.title == title).count()


@pytest.fixture(scope="module")
def entry(client, admin):
    with freeze_business_date(TODAY):
        resp = client.post("/api/knowledge", headers=admin, json={
            "category": "drug_policy", "title": "P21668 国家基本药物目录使用管理办法", "body": "第一条",
            "expire_date": "2026-03-20"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _patch(client, admin, entry_id: int, body: dict):
    with freeze_business_date(TODAY):
        return client.patch(f"/api/knowledge/{entry_id}", headers=admin, json=body)


def test_续期到昨天_422且库不变(client, admin, entry):
    resp = _patch(client, admin, entry, {"expire_date": "2026-03-09"})
    assert resp.status_code == 422, resp.text                      # 修前 200
    assert resp.json()["detail"] == "有效期至（2026-03-09）不得早于今天"
    assert _row(entry)[0] == "2026-03-20"
    # 修前这一步之后，默认检索与临期提醒里都没有这一条
    listed = client.get("/api/knowledge", headers=admin, params={"q": "P21668", "today": TODAY.isoformat()}).json()
    assert [k["id"] for k in listed] == [entry]
    expiring = client.get("/api/knowledge/expiring", headers=admin, params={"today": TODAY.isoformat()}).json()
    assert entry in [k["id"] for k in expiring]


def test_续期到去年同一天也拦(client, admin, entry):
    resp = _patch(client, admin, entry, {"expire_date": "2025-03-20"})
    assert resp.status_code == 422, resp.text
    assert _row(entry)[0] == "2026-03-20"


def test_续期到今天照收_仍在临期提醒里(client, admin, entry):
    resp = _patch(client, admin, entry, {"expire_date": TODAY.isoformat()})
    assert resp.status_code == 200, resp.text
    assert resp.json()["expire_date"] == "2026-03-10"
    expiring = client.get("/api/knowledge/expiring", headers=admin, params={"today": TODAY.isoformat()}).json()
    assert entry in [k["id"] for k in expiring]
    # 往后续期、改为长期有效（空串）照收
    assert _patch(client, admin, entry, {"expire_date": "2027-03-10"}).status_code == 200
    assert _patch(client, admin, entry, {"expire_date": ""}).status_code == 200
    assert _row(entry)[0] == ""


def test_发布填昨天_422且不落库(client, admin):
    title = "P21668 发布即过期的院感制度"
    with freeze_business_date(TODAY):
        resp = client.post("/api/knowledge", headers=admin, json={
            "category": "regulation", "title": title, "expire_date": "2026-03-09"})
    assert resp.status_code == 422, resp.text                      # 修前 201，之后默认检索里没有它
    assert resp.json()["detail"] == "有效期至（2026-03-09）不得早于今天"
    assert _count(title) == 0
    with freeze_business_date(TODAY):
        today = client.post("/api/knowledge", headers=admin, json={
            "category": "regulation", "title": title, "expire_date": TODAY.isoformat()})
        forever = client.post("/api/knowledge", headers=admin, json={"category": "regulation", "title": title})
    assert today.status_code == 201 and forever.status_code == 201  # 当天到期、长期有效照收
    assert _count(title) == 2


def test_不带有效期的PATCH照旧_存量已过期的行能停用(client, admin):
    with SessionLocal() as db:
        operator = db.query(User).filter(User.username == "admin").one().id
        stale = KnowledgeEntry(category="regulation", title="P21668 存量已过期制度", body="旧正文",
                               expire_date="2020-01-01", created_by=operator)
        db.add(stale)
        db.commit()
        stale_id = stale.id
    revised = _patch(client, admin, stale_id, {"body": "新正文"})
    assert revised.status_code == 200, revised.text
    off = _patch(client, admin, stale_id, {"active": False})
    assert off.status_code == 200, off.text
    assert _row(stale_id) == ("2020-01-01", False, "新正文")         # 存量的有效期不动
