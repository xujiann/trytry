"""慢专病报告的考核排名段按名次取前 20，与卫健工作台同一句（P2-638，第十四批「导出 / 打印 vs 页面」扫描 R1-1）。

报告段落原先按写入顺序倒着取最新 20 条：分数按对象顺序写入、写完才排名次——对象多于 20 个时，名次最前的几个
正好落在截掉的那一截里，列出来的还是倒序；同一方案同一周期，卫健工作台按名次取前 20（P2-551），两边对不上。
默认模板「慢专病考核月报」就用这一段。截断了也要说：段落带「共 N 个，列前 20 名」。
"""
import pytest

PERIOD = "2026-10"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdAssessPlan, SpdScore

    with SessionLocal() as db:
        big, small = SpdAssessPlan(code="P2638_BIG", name="P2638 大方案"), SpdAssessPlan(code="P2638_S", name="P2638 小方案")
        db.add_all([big, small])
        db.flush()
        for i in range(3):   # 先算的一期：3 个对象，不截断
            db.add(SpdScore(plan_id=small.id, period="2026-09", object_type="org", object_id=2000 + i,
                            object_name=f"P2638 小{i}", total_score=90 - i, rank=i + 1))
        db.flush()
        # 最近算的一期：25 个对象，写入顺序 = 对象顺序，名次与写入顺序无关（第 i 个写入的名次是 (7i mod 25) + 1）
        for i in range(25):
            db.add(SpdScore(plan_id=big.id, period=PERIOD, object_type="org", object_id=1000 + i,
                            object_name=f"P2638 对象{i:02d}", total_score=100 - (7 * i % 25), rank=7 * i % 25 + 1))
        db.commit()


def _section(period):
    from app.database import SessionLocal
    from app.spd.reporting import compose_section

    with SessionLocal() as db:
        return compose_section(db, {"key": "score", "title": "考核排名", "period": period}, None, "monthly")


def test_按名次取前20_截断写明(world):
    out = _section(PERIOD)
    assert [row[3] for row in out["rows"]] == list(range(1, 21)), out["rows"]   # 修前名次 [25, 24, …] 里缺了前几名
    assert out["note"] == "共 25 个考核对象，列前 20 名"


def test_与卫健工作台同一批人同一顺序(client, admin, world):
    workbench = client.get("/api/spd/workbench/health-commission", headers=admin).json()["scores"]
    assert {s["period"] for s in workbench} == {PERIOD}   # 工作台取最近算的那一次，正是截断的这一期
    out = _section(PERIOD)
    assert [(r[0], r[3]) for r in out["rows"]] == [(s["object_name"], s["rank"]) for s in workbench]


def test_没截断的不写说明(world):
    out = _section("2026-09")
    assert [row[3] for row in out["rows"]] == [1, 2, 3] and "note" not in out
