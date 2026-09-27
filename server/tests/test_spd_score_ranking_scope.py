"""卫健工作台的考核排名按机构范围、一方案一周期出（P2-551，第十批「同源数字承诺」扫描 X2-7）。

报告的考核排名段落早就按机构与周期过滤（P2-15 / P2-103）；卫健工作台的排名却是全县所有分数按名次排、取前 20：
各方案、各周期的第 1 名排在一起，指定了机构也照样列出别家。修后与报告共用 `score_in_orgs`，只出范围内最近算的那一次
考核；报告段落同一周期有几套方案时也只出最近算的那一套。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdAssessPlan, SpdScore

    orgs = {tag: client.post("/api/organizations", headers=admin, json={
        "name": f"P2551 {tag}院", "org_type": "township", "level": "township"}).json()["id"] for tag in ("甲", "乙")}
    with SessionLocal() as db:
        plans = [SpdAssessPlan(code=f"P2551_{n}", name=f"P2551 方案{n}") for n in range(4)]
        db.add_all(plans)
        db.flush()
        rows = [   # (方案, 周期, 机构, 名次, 分)——按录入先后即考核先后
            (0, "2026-07", "甲", 1, 90.0), (0, "2026-07", "乙", 2, 80.0),
            (1, "2026-08", "乙", 1, 95.0), (1, "2026-08", "甲", 2, 70.0),
            (2, "2026-09", "甲", 1, 60.0), (3, "2026-09", "甲", 1, 65.0),
        ]
        for plan, period, tag, rank, score in rows:
            db.add(SpdScore(plan_id=plans[plan].id, period=period, object_type="org", object_id=orgs[tag],
                            object_name=f"P2551 {tag}院", total_score=score, rank=rank))
            db.flush()
        db.commit()
    return orgs


def _ranking(client, admin, **params):
    resp = client.get("/api/spd/workbench/health-commission", headers=admin, params=params)
    assert resp.status_code == 200, resp.text
    return [(s["object_name"], s["period"], s["rank"]) for s in resp.json()["scores"]]


def test_指定机构只出本机构最近一次考核(client, admin, world):
    # 修前：四个周期、两家机构的分数按名次混排，乙院也在内
    assert _ranking(client, admin, org_id=world["甲"]) == [("P2551 甲院", "2026-09", 1)]


def test_报告段落同一周期几套方案只出最近算的那套(world):
    from app.database import SessionLocal
    from app.spd.reporting import compose_section

    with SessionLocal() as db:
        out = compose_section(db, {"key": "score", "title": "考核排名", "period": "2026-09"}, world["甲"], "monthly")
    assert out["rows"] == [["P2551 甲院", "2026-09", 65.0, 1]]   # 修前两套方案的「第 1 名」都在表里
