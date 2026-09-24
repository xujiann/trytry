"""慢专病考核期（`period`）的写法校验：跑分与工作量统计（P1-62）。

考核期有三种合法写法：年度 `2026`、季度 `2026-Q3`、月度 `2026-08`——跑分弹窗与
工作量筛选框的占位符都这么写，二者都是**自由文本框**。修复前 `assess._period_range`
手写解析、不做任何校验：

- `2026/08`、`abc`、`2026-Qx` → 未捕获的 ValueError，**跑分与工作量都是 500**
  （SQLite 上就炸，不必等真 PG）；
- `2026-13`、`2026-Q5` → 解析出 `2026-13-01` 这类区间，**照常出分并写库**；
- `2026-8`（不补零）→ 区间起点 `2026-8-01` 在字典序上大于整个 8 月，
  **全部对象记零分并写库**，以 `2026-8` 为期的一套"考核结果"就此存在。

考核结果是拿去排名、兑现绩效的东西。现在三种写法走同一个校验：月度那种复用
`datetypes.check_month`（月度期间的唯一真源），年度与季度各一条形状。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

B = "/api/spd"


@pytest.fixture(scope="module")
def client():
    """raise_server_exceptions=False：要断言的正是"会不会出 500"。"""
    reset_database()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def auth(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def plan(client, auth):
    org = client.post(
        "/api/organizations",
        json={"name": "考核期校验卫生院", "org_type": "township", "level": "township"},
        headers=auth,
    ).json()
    indicator = client.post(
        f"{B}/indicators",
        json={"code": "prd_done_rate", "name": "考核期校验完成率", "data_source": "task",
              "object_type": "org", "formula": "done / total * 100", "target_value": 100,
              "score_rule": {"type": "ratio", "full": 100, "target": 100}},
        headers=auth,
    )
    assert indicator.status_code == 201, indicator.text
    created = client.post(
        f"{B}/assess-plans",
        json={"code": "prd_plan", "name": "考核期校验方案", "level": "township",
              "object_type": "org", "period_type": "month",
              "items": [{"indicator_code": "prd_done_rate", "weight": 100}]},
        headers=auth,
    )
    assert created.status_code == 201, created.text
    return {"id": created.json()["id"], "org_id": org["id"]}


#: 修复前：前三个 500，后三个 200 且写库。
BAD_PERIODS = ["2026/08", "abc", "2026-Qx", "2026-13", "2026-Q5", "2026-8"]
GOOD_PERIODS = ["2026", "2026-Q3", "2026-08"]


def _run(client, auth, plan, period):
    return client.post(
        f"{B}/scores/run",
        json={"plan_id": plan["id"], "period": period, "object_ids": [plan["org_id"]]},
        headers=auth,
    )


@pytest.mark.parametrize("bad", BAD_PERIODS)
def test_跑分的考核期写错_422且不写库(client, auth, plan, bad):
    resp = _run(client, auth, plan, bad)
    assert resp.status_code == 422, (bad, resp.status_code, resp.text[:200])
    assert resp.json()["detail"][0]["loc"] == ["body", "period"]
    rows = client.get(f"{B}/scores", params={"plan_id": plan["id"], "period": bad}, headers=auth)
    assert rows.status_code == 200 and rows.json() == [], f"{bad} 被拒之后不该留下考核结果"


@pytest.mark.parametrize("good", GOOD_PERIODS)
def test_跑分的三种合法写法照常(client, auth, plan, good):
    resp = _run(client, auth, plan, good)
    assert resp.status_code == 200, (good, resp.text)


@pytest.mark.parametrize("bad", BAD_PERIODS)
def test_工作量统计的考核期写错_422而不是500(client, auth, bad):
    resp = client.get(f"{B}/workload", params={"period": bad}, headers=auth)
    assert resp.status_code == 422, (bad, resp.status_code, resp.text[:200])
    assert resp.json()["detail"].startswith("period："), resp.json()


@pytest.mark.parametrize("good", GOOD_PERIODS)
def test_工作量统计的三种合法写法照常(client, auth, good):
    resp = client.get(f"{B}/workload", params={"period": good}, headers=auth)
    assert resp.status_code == 200, (good, resp.text)
    assert resp.json()["period"] == good


def test_工作量统计留空取本月_行为未变(client, auth):
    blank = client.get(f"{B}/workload", params={"period": ""}, headers=auth)
    absent = client.get(f"{B}/workload", headers=auth)
    assert blank.status_code == absent.status_code == 200
    assert blank.json()["period"] == absent.json()["period"]
