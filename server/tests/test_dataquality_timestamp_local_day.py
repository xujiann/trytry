"""数据质控拿时间戳与日期比时先换本地日期（P2-714，第十八批「字符串比较当数值 / 日期」扫描 V4-2）。

「结束不得早于开始」（`_check_datetime_order`）起止任一是字符串就整个按 `str()` 比。P2-234 自己举的「下次随访日不得
早于建档时间」（起 `created_at` 时间戳、止 `next_due` 日期串）里，下次随访日定在建档当天——`"2026-09-20" <
"2026-09-20 05:20:37"` 恒真，报违规。起 `onset_date`、止 `reported_at` 的：东八区 0–8 点报的卡落库是前一天的 UTC，
被判「报告早于发病」。违规文案印的也是落库的 UTC 时刻。「日期不得晚于今天」（`_check_date_not_future`）拿时间戳的
UTC 日期比本地的今天：东八区明天 0–8 点的时刻算成今天、漏报。同形状的报卡迟报早就按「先换本地日期」修过（P2-528）。

修法：一边只到日期、一边是时间戳时，时间戳先换本地日期、按日粒度比，同一天不算早于；两边同为时间戳的照旧按时刻比；
文案一律印本地时刻。「不晚于今天」取时间戳的本地日期。时区钉在东八区，用例不随跑的机器变。
"""
import itertools
import time
from datetime import datetime, timedelta, timezone
from datetime import time as clock_time

import pytest

from app import clock
from app.database import SessionLocal
from app.models import ChronicPatient, InfectiousCase

_SEQ = itertools.count(1)


@pytest.fixture
def east_eight(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        yield
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P2714 卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _run(client, admin, table, config):
    """建一条临时逻辑规则跑一遍，返回 {记录号: 违规文案}，跑完删掉（别留给别的用例的汇总）。"""
    code = f"P2714_{next(_SEQ)}"
    rule = client.post("/api/dataquality/rules", headers=admin, json={
        "code": code, "name": "P2714 临时规则", "target_table": table, "rule_type": "logic",
        "config": config, "severity": "warn"})
    assert rule.status_code == 201, rule.text
    try:
        run = client.get(f"/api/dataquality/run?rule_code={code}&limit=1000", headers=admin)
        assert run.status_code == 200, run.text
        return {item["record_id"]: item["message"] for item in run.json()["items"]}
    finally:
        client.delete(f"/api/dataquality/rules/{rule.json()['id']}", headers=admin)


def _cases(org, *reported_ats, created_at=None):
    """直接落库几张报卡（病种编码专用，不进别的用例按病种数的多点预警），返回记录号。"""
    with SessionLocal() as db:
        rows = [InfectiousCase(org_id=org, disease_code="P2714", disease_name="P2714 用例病种", onset_date="2026-01-11",
                               reported_at=reported_at, **({"created_at": created_at} if created_at else {}))
                for reported_at in reported_ats]
        db.add_all(rows)
        db.commit()
        return [row.id for row in rows]


def test_下次随访日定在建档当天不算早于_前一天照报(client, admin, org, east_eight):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2714 慢病", "id_card": "330127196808082714"}).json()["id"]
    created = datetime(2026, 9, 20, 5, 20, 37)   # 本地 09-20 13:20
    with SessionLocal() as db:
        same_day = ChronicPatient(patient_id=patient, disease="hypertension", managed_by_org_id=org,
                                  next_due="2026-09-20", created_at=created)
        day_before = ChronicPatient(patient_id=patient, disease="diabetes", managed_by_org_id=org,
                                    next_due="2026-09-19", created_at=created)
        db.add_all([same_day, day_before])
        db.commit()
        ids = (same_day.id, day_before.id)
    hits = _run(client, admin, "chronic_patients",
                {"check": "datetime_order", "start_field": "created_at", "end_field": "next_due"})
    assert ids[0] not in hits, hits   # 修前："2026-09-20" < "2026-09-20 05:20:37" 恒真，报违规
    assert hits[ids[1]] == "next_due（2026-09-19）早于 created_at（2026-09-20 13:20）", hits


def test_东八区早上报的卡不判报告早于发病_真早于的照报且印本地时刻(client, admin, org, east_eight):
    # 本地 01-11 07:30（与发病同一天）/ 01-10 07:30（真早于发病）
    morning, day_before = _cases(org, datetime(2026, 1, 10, 23, 30), datetime(2026, 1, 9, 23, 30))
    hits = _run(client, admin, InfectiousCase.__tablename__,
                {"check": "datetime_order", "start_field": "onset_date", "end_field": "reported_at"})
    assert morning not in hits, hits   # 修前：UTC 01-10 23:30 早于 01-11，判「报告早于发病」
    assert hits[day_before] == "reported_at（2026-01-10 07:30）早于 onset_date（2026-01-11）", hits   # 修前印 UTC 时刻


def test_两边都是时间戳照旧按时刻比_文案印本地时刻(client, admin, org, east_eight):
    (case,) = _cases(org, datetime(2026, 1, 10, 23, 30), created_at=datetime(2026, 1, 10, 23, 0))
    (same,) = _cases(org, datetime(2026, 1, 10, 23, 30), created_at=datetime(2026, 1, 10, 23, 30))
    hits = _run(client, admin, InfectiousCase.__tablename__,
                {"check": "datetime_order", "start_field": "reported_at", "end_field": "created_at"})
    assert same not in hits, hits
    # 修前：created_at（2026-01-10 23:00）早于 reported_at（2026-01-10 23:30）——东八区的人看着差 8 小时
    assert hits[case] == "created_at（2026-01-11 07:00）早于 reported_at（2026-01-11 07:30）", hits


def test_不晚于今天按本地日期判_东八区明天凌晨的时刻照报(client, admin, org, east_eight):
    today = clock.today()

    def utc_of(day, hour):   # 本地某天某时 → 落库口径（naive UTC）
        return datetime.combine(day, clock_time(hour)).astimezone(timezone.utc).replace(tzinfo=None)

    tomorrow_small_hours, this_morning = _cases(org, utc_of(today + timedelta(days=1), 1), utc_of(today, 7))
    hits = _run(client, admin, InfectiousCase.__tablename__, {"check": "date_not_future", "field": "reported_at"})
    # 修前：本地明天 01:00 落库是今天 17:00 UTC，取 UTC 日期算成今天，漏报
    assert hits.get(tomorrow_small_hours) == (
        f"reported_at（{(today + timedelta(days=1)).isoformat()}）晚于当前日期（{today.isoformat()}）"), hits
    assert this_morning not in hits, hits
