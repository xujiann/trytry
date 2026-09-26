"""慢专病团队清单按病种筛在分页之前：总数与这一页都按筛过的算（P2-177）。

原先先分页、再在这一页里挑管这个病种的团队——`X-Total-Count` 是没筛的总数，这一页少几条；管这个病种的团队
排在第一页之后，就整个看不见（前端只取一页）。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def seeded(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdTeam

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2177 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    with SessionLocal() as db:
        others = [SpdTeam(name=f"P2177 糖尿病团队{i}", org_id=org, level="township", program_codes=["diabetes"])
                  for i in range(3)]
        db.add_all(others)
        target = SpdTeam(name="P2177 高血压团队", org_id=org, level="township", program_codes=["hypertension", "diabetes"])
        db.add(target)
        db.commit()
        return {"org": org, "target": target.id}


def test_按病种筛在分页之前(client, admin, seeded):
    resp = client.get(f"{B}/teams?org_id={seeded['org']}&program_code=hypertension&limit=2", headers=admin)
    assert resp.status_code == 200, resp.text
    assert [t["id"] for t in resp.json()] == [seeded["target"]]   # 修前 []：它排在第 4 个，第一页（2 条）里没有
    assert resp.headers["X-Total-Count"] == "1"                     # 修前 4：没筛的总数


def test_不带病种照旧分页(client, admin, seeded):
    resp = client.get(f"{B}/teams?org_id={seeded['org']}&limit=2", headers=admin)
    assert len(resp.json()) == 2 and resp.headers["X-Total-Count"] == "4"
