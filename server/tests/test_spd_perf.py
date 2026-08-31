"""P2-1 的守卫用例：SQL 条数不随对象/患者规模线性增长。

不是压测——压测数字随机器波动，进不了 CI。这里数的是**SQL 语句条数**：
N+1 的本质是"对象多一个、查询多一条"，那么固定操作、把规模翻倍，
查询条数不变（或只差常数）就是结构性的证据。
"""
from contextlib import contextmanager

import pytest
from sqlalchemy import event

from app.database import engine

from conftest import login


@pytest.fixture(scope="module")
def h(client):
    return login(client, "admin", "admin123")


@contextmanager
def count_sql():
    """统计代码块内执行的 SQL 语句条数。"""
    counter = {"n": 0}

    def _tick(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _tick)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _tick)


def _make_org(client, h, name):
    return client.post(
        "/api/organizations",
        json={"name": name, "org_type": "township", "level": "township"},
        headers=h,
    ).json()


def _make_patients(client, h, prefix, start, count):
    return [
        client.post(
            "/api/patients",
            json={"name": f"{prefix}{i}", "id_card": f"33098119700707{i:04d}",
                  "gender": "男", "birth_date": "1970-07-07"},
            headers=h,
        ).json()
        for i in range(start, start + count)
    ]


def _enroll(client, h, patient_id, org_id, doctor_user_id=None):
    body = {"patient_id": patient_id, "program_code": "hypertension", "org_id": org_id}
    if doctor_user_id:
        body["doctor_user_id"] = doctor_user_id
    resp = client.post("/api/spd/enrollments", json=body, headers=h)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_team_workbench_query_count_constant_in_patients(client, h):
    """工作台的查询条数不随在管患者数增长（待评估/待入径改为 IN 查询）。"""
    org = _make_org(client, h, "性能卫生院A")
    first = _make_patients(client, h, "性能患者", 0, 5)
    for p in first:
        _enroll(client, h, p["id"], org["id"], doctor_user_id=1)

    client.get("/api/spd/workbench/team?role=member", headers=h)  # 预热（首次会建缓存等）
    with count_sql() as small:
        assert client.get("/api/spd/workbench/team?role=member", headers=h).status_code == 200

    more = _make_patients(client, h, "性能患者", 5, 20)
    for p in more:
        _enroll(client, h, p["id"], org["id"], doctor_user_id=1)
    with count_sql() as big:
        assert client.get("/api/spd/workbench/team?role=member", headers=h).status_code == 200

    assert big["n"] <= small["n"] + 2, (
        f"患者从 5 涨到 25，工作台查询从 {small['n']} 涨到 {big['n']}——又出现逐患者查询了"
    )


def test_run_scoring_query_count_constant_in_objects(client, h):
    """整表计分的查询条数不随考核对象数增长（批量取数 GROUP BY）。"""
    plan = client.post(
        "/api/spd/assess-plans",
        json={"code": "perf_plan", "name": "性能考核", "level": "township",
              "object_type": "org", "period_type": "month",
              "items": [{"indicator_code": "followup_rate", "weight": 50},
                        {"indicator_code": "referral_closure_rate", "weight": 50}]},
        headers=h,
    ).json()
    org_small = [_make_org(client, h, f"计分机构{i}")["id"] for i in range(2)]

    def run(ids):
        resp = client.post(
            "/api/spd/scores/run",
            json={"plan_id": plan["id"], "period": "2026-08", "object_ids": ids},
            headers=h,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    run(org_small)  # 预热
    with count_sql() as small:
        run(org_small)

    org_big = org_small + [_make_org(client, h, f"计分机构{i}")["id"] for i in range(2, 10)]
    with count_sql() as big:
        result = run(org_big)
    assert result["scored"] == 10

    # 对象从 2 涨到 10：取数部分必须与对象数无关（每个指标一条 GROUP BY），
    # 允许的增长只来自写回——每对象 SAVEPOINT+SELECT+INSERT/UPDATE ≈ 6 条。
    allowed = small["n"] + (10 - 2) * 6
    assert big["n"] <= allowed, (
        f"对象从 2 涨到 10，计分查询从 {small['n']} 涨到 {big['n']}（上限 {allowed}）"
        "——取数又回到逐对象查询了"
    )
