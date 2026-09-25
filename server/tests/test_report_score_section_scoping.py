"""报告"考核排名"段落必须按机构与周期过滤。

修的 bug：`reporting._score` 的渲染器签名收了 `org_id` 与 `period`，
但**两个都没用上**——`db.query(SpdScore).order_by(id.desc()).limit(20)`。
于是任何机构、任何周期的报告，这一段都是同一份「最近 20 条」：
甲机构的报告里印着乙机构的排名，一季度的报告里印着二季度的分数。

`spd_scores` 上没有 org_id 列（考核对象有机构/团队/村医/医师四类），
所以按 `object_type` 分派去查归属机构，见 `_SCORE_OBJECT_ORG`。
口径取"恰好属于该机构"而非"含下级"——与本模块其余段落一致。
"""
import pytest

from conftest import reset_database

from app.database import SessionLocal
from app.models import Organization, User
from app.spd.models import SpdAssessPlan, SpdScore, SpdTeam
from app.spd.reporting import _score

SECTION = {"key": "score", "title": "考核排名"}


@pytest.fixture()
def world():
    """两家机构，各有：机构本身的分、团队的分、医师的分；两个周期。"""
    reset_database()
    with SessionLocal() as db:
        made = {}
        plan = SpdAssessPlan(code="SCOPE-PLAN", name="排名节考核方案")   # 分数挂在真实的方案上（开发库开了外键约束（P2-71））
        db.add(plan)
        db.flush()
        for tag in ("甲", "乙"):
            org = Organization(name=f"{tag}考核院", org_type="lead_hospital", level="county")
            db.add(org)
            db.flush()
            team = SpdTeam(name=f"{tag}团队", org_id=org.id, level="county")
            doctor = User(username=f"{tag}医师", password_hash="x", role="doctor", org_id=org.id)
            db.add_all([team, doctor])
            db.flush()
            for period in ("2026Q1", "2026Q2"):
                db.add_all([
                    SpdScore(plan_id=plan.id, period=period, object_type="org",
                             object_id=org.id, object_name=f"{tag}院-{period}", total_score=90),
                    SpdScore(plan_id=plan.id, period=period, object_type="team",
                             object_id=team.id, object_name=f"{tag}团队-{period}", total_score=80),
                    SpdScore(plan_id=plan.id, period=period, object_type="doctor",
                             object_id=doctor.id, object_name=f"{tag}医师-{period}", total_score=70),
                ])
            made[tag] = {"org_id": org.id, "team_id": team.id, "doctor_id": doctor.id}
        db.commit()
        return made


def _names(org_id, period):
    with SessionLocal() as db:
        return {row[0] for row in _score(db, SECTION, org_id, period)["rows"]}


def test_只出本机构的考核对象(world):
    """机构本身、它的团队、它的医师都要出；别家的一个都不能出。"""
    got = _names(world["甲"]["org_id"], "2026Q1")
    assert got == {"甲院-2026Q1", "甲团队-2026Q1", "甲医师-2026Q1"}
    assert not any(n.startswith("乙") for n in got), "报告里印出了别家机构的排名"


def test_只出本周期(world):
    got = _names(world["甲"]["org_id"], "2026Q2")
    assert got == {"甲院-2026Q2", "甲团队-2026Q2", "甲医师-2026Q2"}


def test_不给机构时不按机构过滤(world):
    """全域报告（org_id=None）看全部——但周期仍然要过滤。"""
    got = _names(None, "2026Q1")
    assert got == {
        "甲院-2026Q1", "甲团队-2026Q1", "甲医师-2026Q1",
        "乙院-2026Q1", "乙团队-2026Q1", "乙医师-2026Q1",
    }


def test_机构名下没有考核对象时返回空而不是全域(world):
    """空结果要空着——退回全域数据比少一段更糟：那是**别家的**数字。"""
    with SessionLocal() as db:
        empty = Organization(name="空考核院", org_type="township", level="township")
        db.add(empty)
        db.commit()
        empty_id = empty.id
    assert _names(empty_id, "2026Q1") == set()


def test_按模板频率出的报告_取本机构最近一期_不再恒为空(world):
    """P2-103：真正的调用方（定时推送 `jobs.spd_report_push`、手工生成 `POST /report-instances`）传进来的是模板的
    **频率关键字**（daily / weekly / monthly），不是考核周期值——原先拿它去和分数的周期（2026Q1 / 2026-08）等值比，
    推送出去的每一份报告这一段都是空的（上面几条直接喂周期值调渲染器，看不出来）。按频率出的报告取本机构范围内
    最近算出的一期：只出一期，不混周期。"""
    from app.spd.reporting import compose_section

    with SessionLocal() as db:
        rows = compose_section(db, SECTION, world["甲"]["org_id"], "monthly")["rows"]
        assert {r[0] for r in rows} == {"甲院-2026Q2", "甲团队-2026Q2", "甲医师-2026Q2"}   # 修前空
        everywhere = {r[0] for r in compose_section(db, SECTION, None, "weekly")["rows"]}
        assert everywhere == {"甲院-2026Q2", "甲团队-2026Q2", "甲医师-2026Q2", "乙院-2026Q2", "乙团队-2026Q2", "乙医师-2026Q2"}


def test_段落写明周期的按段落的(world):
    from app.spd.reporting import compose_section

    with SessionLocal() as db:
        rows = compose_section(db, {**SECTION, "period": "2026Q1"}, world["甲"]["org_id"], "monthly")["rows"]
    assert {r[0] for r in rows} == {"甲院-2026Q1", "甲团队-2026Q1", "甲医师-2026Q1"}
