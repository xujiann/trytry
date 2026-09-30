"""基金结余分配的「本次快照参数」把分配时刻写成 UTC 钟点（P2-970，第二十七批「UTC 时间戳与本地业务日混用」扫描 G2-6）。

`distribute` 把 `at=<utcnow>` 写进 `fund_settlements.score_basis`，页面原样印「本次快照参数：…」：东八区部署下本地 10:15 做的
分配写成 `at=… 02:15`。它是落库的文字，以后前端统一换算显示也换不到它。`clock.now_local()` 写明给人看的字符串用本地时刻，
同类的 P2-171 / P2-455 / P2-535 都已改成本地。

修法：改取 `now_local()`；存量不动。
"""
import re
import time
from datetime import datetime, timedelta

import pytest

from app.clock import now_local, now_naive


@pytest.fixture
def east_eight(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        yield
    finally:
        monkeypatch.undo()
        time.tzset()


def test_东八区分配_快照参数里的时刻是本地钟点(client, admin, east_eight):
    year = now_naive().year
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2970 卫生院", "org_type": "township", "level": "township"})
    assert org.status_code in (200, 201), org.text
    pool = client.post("/api/fund/pools", headers=admin, json={
        "year": year, "insurance_type": "resident", "total_amount": 100000.0})
    assert pool.status_code == 201, pool.text
    pool_id = pool.json()["id"]
    closed = client.post(f"/api/fund/pools/{pool_id}/periods", headers=admin,
                         json={"period": f"{year}-06", "actual_amount": 80000})
    assert closed.status_code == 201, closed.text
    settled = client.post(f"/api/fund/pools/{pool_id}/settle", headers=admin, json={})
    assert settled.status_code == 201, settled.text
    before = now_local()
    resp = client.post(f"/api/fund/pools/{pool_id}/distribute", headers=admin, json={"formula_expr": "1"})
    after = now_local()
    assert resp.status_code == 200, resp.text
    at = datetime.strptime(re.search(r"at=(\d{4}-\d\d-\d\d \d\d:\d\d)", resp.json()["score_basis"]).group(1),
                           "%Y-%m-%d %H:%M")
    assert before.replace(second=0, microsecond=0) <= at <= after   # 修前是 UTC 钟点，早 8 小时
    assert abs(now_naive() + timedelta(hours=8) - after) < timedelta(minutes=1)   # 前提：进程确实在东八区
