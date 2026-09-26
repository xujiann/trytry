"""报告实例「我的」在分页之后才筛：总数是没筛的，订阅我的报告排在第一页之后就整个看不见（P2-295）。

`GET /api/spd/report-instances?mine=true` 原先先按 `offset / limit` 取一页，再在这一页里挑「没订阅人或订阅了我」的——
`X-Total-Count` 是没筛的总数、页里少几条；别人的报告多几页，我的就被挤到后面、第一页是空的。

修法：先挑出编号、再交回库里分页（与团队清单按病种筛 P2-177 同一个做法）。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"


def _login(client, username, password="pw123456"):
    token = client.post("/api/auth/login", json={"username": username, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    from app.models import User
    from app.spd.models import SpdReportInstance

    created = client.post("/api/users", headers=admin, json={
        "username": "p2295_doc", "password": "pw123456", "full_name": "P2295 医生", "role": "doctor"})
    assert created.status_code == 201, created.text
    with SessionLocal() as db:
        me = db.query(User.id).filter(User.username == "p2295_doc").scalar()
        other = db.query(User.id).filter(User.username == "admin").scalar()
        mine = SpdReportInstance(title="P2295 我的周报", template_code="P2295_T", subscriber_ids=[me])
        db.add(mine)
        db.flush()
        for n in range(3):   # 编号更大、排在前面的三份别人的
            db.add(SpdReportInstance(title=f"P2295 别人的周报 {n}", template_code="P2295_T", subscriber_ids=[other]))
        db.commit()
        return {"mine": mine.id, "headers": _login(client, "p2295_doc")}


def test_我的报告排在别人的后面_第一页照样看得到_总数是筛过的(client, world):
    got = client.get(f"{B}/report-instances", params={"template_code": "P2295_T", "mine": True, "limit": 2},
                     headers=world["headers"])
    assert got.status_code == 200, got.text
    assert [r["id"] for r in got.json()] == [world["mine"]]   # 修前第一页是空的
    assert got.headers["X-Total-Count"] == "1"   # 修前 4


def test_不带mine照旧全列(client, world):
    got = client.get(f"{B}/report-instances", params={"template_code": "P2295_T"}, headers=world["headers"])
    assert got.status_code == 200 and got.headers["X-Total-Count"] == "4", got.text
