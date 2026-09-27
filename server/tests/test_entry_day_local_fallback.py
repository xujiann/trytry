"""「没填日期的按录入那天算」取录入时刻的本地日期（第十五批「时间边界」扫描 S2-6，与 P2-545 同形）。

两处兜底原先直接拿落库时刻（naive UTC）`.date()`：东八区 0–8 点录的算成前一天——
①老年人健康评估没填评估日期的，年度复评提醒提前一天报「已超一年」，「最近一次评估」的先后也按它比；
②孕产妇没登记分娩、产后访视没填日期的，推定的「这一胎结束日」早一天，产后访视当天做的筛查被当成「晚于这一胎」409。
"""
import itertools
import time
from datetime import datetime

import pytest

_CARDS = itertools.count(1)


@pytest.fixture
def east_eight(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        yield
    finally:
        monkeypatch.undo()
        time.tzset()


def _patient(client, admin, gender="男"):
    resp = client.post("/api/patients", headers=admin, json={
        "name": "S2-6 居民", "id_card": f"33010619550606{2600 + next(_CARDS):04d}", "gender": gender})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _set_created_at(model, row_id, moment):
    from app.database import SessionLocal

    with SessionLocal() as db:
        db.get(model, row_id).created_at = moment
        db.commit()


def test_没填评估日期的按本地录入日满一年才提醒复评(client, admin, east_eight):
    from app.models import ElderlyAssessment

    patient = _patient(client, admin)
    resp = client.post("/api/eldercare/assessments", headers=admin, json={"patient_id": patient, "adl_score": 95})
    assert resp.status_code == 201, resp.text
    # 本地 2030-09-28 早上 07:00 录入、没填评估日期——落库是前一天 UTC 23:00
    _set_created_at(ElderlyAssessment, resp.json()["id"], datetime(2030, 9, 27, 23, 0))

    def due(today):
        body = client.get("/api/eldercare/alerts", headers=admin, params={"today": today}).json()
        return [a for a in body["alerts"] if a["patient_id"] == patient and a["alert_type"] == "reassess_due"]

    assert due("2031-09-27") == []            # 修前这一天就报「已超一年」，早了一天
    assert len(due("2031-09-28")) == 1


def test_产后访视没填日期的按本地录入日推这一胎结束日(client, admin, east_eight):
    from app.models import MaternalVisit

    patient = _patient(client, admin, gender="女")
    record = client.post("/api/maternal/records", headers=admin, json={"patient_id": patient}).json()["id"]
    visit = client.post(f"/api/maternal/records/{record}/visits", headers=admin, json={"visit_type": "postpartum"})
    assert visit.status_code == 201, visit.text
    # 本地 2031-03-10 早上 07:30 做的产后访视、没填访视日期——落库是前一天 UTC 23:30
    _set_created_at(MaternalVisit, visit.json()["id"], datetime(2031, 3, 9, 23, 30))
    same_day = client.post("/api/maternal/screenings", headers=admin, json={
        "record_id": record, "screen_type": "ultrasound", "screen_date": "2031-03-10"})
    assert same_day.status_code == 201, same_day.text   # 修前 409「晚于这一胎的产后访视日期 2031-03-09」
    next_day = client.post("/api/maternal/screenings", headers=admin, json={
        "record_id": record, "screen_type": "ultrasound", "screen_date": "2031-03-11"})
    assert next_day.status_code == 409 and "产后访视日期 2031-03-10" in next_day.json()["detail"], next_day.text
