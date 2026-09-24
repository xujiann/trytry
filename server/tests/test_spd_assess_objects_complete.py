"""考核计分不再只算前 500 个考核对象（P1-84）。

`POST /api/spd/scores/run` 不给 `object_ids` 时按方案层级「全量展开」考核对象（`_objects_of` 的
docstring 原话），可四个分支（机构 / 团队 / 村医 / 医生）都是 `.limit(500).all()`，而且没有排序：

- 村卫生室、村医、医生过 500 的县，**多出来的那些人没有分数**，响应里 `scored` 照样报 500，
  没有任何提示；
- 排名只在这 500 个里排——第 1 名未必是第 1 名；
- 没排序意味着哪 500 个是数据库说了算，重跑同一周期可能换一批：没被选中的那些留着上次的
  分数与名次，同一周期里出现两个第 1 名。

考核结果直接进绩效。修法：去掉上限，按 id 排序展开（并列总分的名次也因此稳定）。
"""
import ast
import inspect
import textwrap

import pytest
from sqlalchemy import insert

from app.database import SessionLocal
from app.models import Organization
from app.spd.routers import assess
import astcode

B = "/api/spd"
N_VILLAGES = 510


@pytest.fixture(scope="module")
def plan(client, admin):
    indicator = client.post(
        f"{B}/indicators",
        json={"code": "p184_count", "name": "对象全量测试计数", "data_source": "task",
              "object_type": "org", "target_value": 10, "score_rule": {"type": "ratio", "full": 100, "target": 10}},
        headers=admin,
    )
    assert indicator.status_code == 201, indicator.text
    created = client.post(
        f"{B}/assess-plans",
        json={"code": "p184_plan", "name": "对象全量测试方案", "level": "village", "object_type": "org",
              "period_type": "month", "items": [{"indicator_code": "p184_count", "weight": 100}]},
        headers=admin,
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _run(client, admin, plan_id, period, **extra):
    r = client.post(f"{B}/scores/run", json={"plan_id": plan_id, "period": period, **extra}, headers=admin)
    assert r.status_code == 200, r.text
    return r.json()


def _scores(client, admin, plan_id, period):
    rows, offset = [], 0
    while True:
        page = client.get(f"{B}/scores", params={"plan_id": plan_id, "period": period, "offset": offset,
                                                 "limit": 500}, headers=admin).json()
        rows += page
        if len(page) < 500:
            return rows
        offset += 500


def test_特征化_给了对象就只算这几个且名次连续(client, admin, plan):
    orgs = [client.post("/api/organizations", json={"name": f"对象全量指定村{i}", "org_type": "village",
                                                     "level": "village"}, headers=admin).json()["id"]
            for i in (1, 2)]
    body = _run(client, admin, plan, "2031-01", object_ids=orgs)
    assert body["scored"] == 2
    rows = _scores(client, admin, plan, "2031-01")
    assert sorted(r["object_id"] for r in rows) == sorted(orgs)
    assert sorted(r["rank"] for r in rows) == [1, 2]


@pytest.fixture(scope="module")
def villages(plan):
    with SessionLocal() as db:
        db.execute(insert(Organization), [
            {"name": f"对象全量村卫生室{i:03d}", "org_type": "village", "level": "village"} for i in range(N_VILLAGES)])
        db.commit()
        return db.query(Organization).filter(Organization.level == "village").count()


def test_村卫生室过500家也一家不落(client, admin, plan, villages):
    assert villages > 500
    body = _run(client, admin, plan, "2031-02")
    assert body["scored"] == villages, "考核对象被截在上限上了——多出来的没有分数"
    rows = _scores(client, admin, plan, "2031-02")
    assert len(rows) == villages
    assert sorted(r["rank"] for r in rows) == list(range(1, villages + 1)), "名次要在全体对象里排、一个不重"


def test_四个分支都不再设上限():
    """机构 / 团队 / 村医 / 医生四个分支原来各有一个 `.limit(500)`，灌量用例只走得到机构那一支。"""
    fn = ast.parse(textwrap.dedent(inspect.getsource(assess._objects_of))).body[0]
    assert ".limit(" not in astcode.code(fn), "docstring 已剥掉，这里看的只是代码"
