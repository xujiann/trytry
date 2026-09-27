"""慢专病积分的每日上限按本地业务日数（P2-534，第十批「日期与期间边界」扫描 X3-6）。

`award_points` 的 docstring：「每日上限按'当天该规则已入账分值'算」；同一套积分的每日签到按 `clock.today()`（本地日历）
记一天。上限却拿流水 `created_at` 的 **UTC 日期**去比本地的今天：东八区 0–8 点入的账 UTC 日期是前一天，
一笔都不算——凌晨签约 12 户照入 12 笔（60 分，上限 50），上午再入 10 笔，同一个本地日 110 分；签到那头照样「今日已签到」。

修后按本地这一天换成的 UTC 区间数（`clock.local_day_utc_range`）。
"""
import time
from datetime import date, datetime, timedelta

import pytest

from conftest import freeze_business_date

DAY = date(2026, 9, 28)


@pytest.fixture
def east_eight(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        with freeze_business_date(DAY):
            yield
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.fixture(scope="module")
def doctors(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2534 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    ids = []
    for key in ("early", "late"):
        resp = client.post("/api/users", headers=admin, json={
            "username": f"p2534_{key}", "password": "pass123456", "role": "doctor", "org_id": org})
        assert resp.status_code == 201, resp.text
        ids.append(resp.json()["id"])
    return {"org": org, "early": ids[0], "late": ids[1]}


def _prefill(db, user_id, org_id, moments):
    """按「签约纳管」规则（种子 pt_sign：每笔 5 分、每日上限 50 分）先记几笔流水，时间戳是 naive UTC。"""
    from app.spd.models import SpdPointRecord
    from app.spd.service import point_account_for

    account = point_account_for(db, user_id, org_id)
    for i, moment in enumerate(moments):
        db.add(SpdPointRecord(account_id=account.id, rule_code="pt_sign", direction="in", points=5,
                              balance_after=5 * (i + 1), ref_type="p2534", ref_id=user_id * 100 + i,
                              note="P2534 预置", created_at=moment))
    db.flush()


def test_本地凌晨入的账算进当天的上限(client, doctors, east_eight):
    from app.database import SessionLocal
    from app.spd.service import award_points

    with SessionLocal() as db:
        # 本地 09-28 01:00–05:30 已入 10 笔 50 分——落库是 UTC 09-27 17:00–21:30
        _prefill(db, doctors["early"], doctors["org"],
                 [datetime(2026, 9, 27, 17) + timedelta(minutes=30 * i) for i in range(10)])
        # 修前：这 10 笔的 UTC 日期是 09-27，一笔不算，照入第 11 笔
        assert award_points(db, doctors["early"], "sign", ref_type="p2534", ref_id=1, org_id=doctors["org"]) is None
        # 本地昨天 23:00 入的 10 笔（UTC 09-27 15:00）不算今天，照常入账
        _prefill(db, doctors["late"], doctors["org"], [datetime(2026, 9, 27, 15, i) for i in range(10)])
        record = award_points(db, doctors["late"], "sign", ref_type="p2534", ref_id=2, org_id=doctors["org"])
        assert record is not None and record.points == 5
        db.rollback()


def test_本地一天换成的_UTC_区间(east_eight):
    from app import clock

    assert clock.local_day_utc_range(DAY) == (datetime(2026, 9, 27, 16), datetime(2026, 9, 28, 16))
