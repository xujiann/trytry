"""得分分析的「高频扣分项」按累计扣分排、不按扣分次数（P2-964，第二十七批「排名、Top-N、并列与空值」扫描 G4-4）。

标题与首列都叫「高频扣分项」，同一张表里还有「扣分次数」一列；取数却按 `total_deduction` 降序截前 10：五家都没有纳管档案、
纳管数指标（权重 10）扣了 5 次共 50 分；只有一家没有办结任务、办结数指标（权重 90）扣了 1 次 90 分——排第一的是只扣过一次的
「办结数」。指标多于 10 个时真正高频的可能被截掉；取数查询不排序，累计扣分并列时谁进前 10 由库决定。

修法：按扣分次数降序，再按累计扣分降序，再按指标编码。
"""
import inspect

import pytest

from app.database import SessionLocal
from app.spd.models import SpdAssessPlan, SpdScore

B = "/api/spd"


@pytest.fixture(scope="module")
def plan(client, admin):
    with SessionLocal() as db:
        made = SpdAssessPlan(code="P2964", name="P2964 方案", items=[])
        db.add(made)
        db.flush()
        for n in range(5):
            detail = [{"indicator_code": "enroll", "indicator_name": "纳管数", "deduction": 10}]
            if n == 0:
                detail.append({"indicator_code": "done", "indicator_name": "办结数", "deduction": 90})
            db.add(SpdScore(plan_id=made.id, period="2026-09", object_id=n + 1, object_name=f"P2964 机构{n}",
                            total_score=100 - sum(d["deduction"] for d in detail), rank=n + 1, detail=detail))
        db.commit()
        return made.id


def test_高频扣分项按扣分次数排(client, admin, plan):
    got = client.get(f"{B}/scores-analysis", headers=admin, params={"plan_id": plan, "period": "2026-09"})
    assert got.status_code == 200, got.text
    top = [(d["indicator_code"], d["count"], d["total_deduction"]) for d in got.json()["top_deductions"]]
    assert top == [("enroll", 5, 50.0), ("done", 1, 90.0)]   # 修前办结数排第一


def test_次数与累计都并列时按指标编码():
    from app.spd.routers.assess import score_analysis

    src = inspect.getsource(score_analysis)
    assert '(-d["count"], -d["total_deduction"], d["indicator_code"])' in src
