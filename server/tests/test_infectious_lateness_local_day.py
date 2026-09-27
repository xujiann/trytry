"""传染病迟报按报告的**本地**日期判（P2-528）。

`reported_at` 落库是 naive UTC，发病日期是临床按当地日历填的。迟报清单与报告卡导出原先都拿 `reported_at.date()`
（UTC 日期）去减发病日期：东八区 0–8 点报的卡，报告日算成前一天、迟报天数少 1——甲类（鼠疫，2 小时）发病次日早上
7 点半才报，清单里没有它、导出写「及时」；8 点半报的那张却是「迟报」。两处还各写了一遍同一个判定。

修后两处共用 `_timeliness`，报告日取 `reported_at` 换成本地时间后的日期。
"""
import csv
import io
import time
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.models import InfectiousCase

ONSET = "2026-01-10"
# 东八区 01-11 07:30 报的（落库 UTC 01-10 23:30）与 08:30 报的（UTC 01-11 00:30）
EARLY_MORNING_UTC = datetime(2026, 1, 10, 23, 30)
AFTER_EIGHT_UTC = datetime(2026, 1, 11, 0, 30)


@pytest.fixture(scope="module")
def cases(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2528 报卡医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ids = []
    for reported_at in (EARLY_MORNING_UTC, AFTER_EIGHT_UTC):
        resp = client.post("/api/infectious/cases", headers=admin, json={
            "org_id": org, "disease_code": "A20", "disease_name": "鼠疫", "onset_date": ONSET})
        assert resp.status_code == 201, resp.text
        ids.append(resp.json()["id"])
        with SessionLocal() as db:
            db.get(InfectiousCase, ids[-1]).reported_at = reported_at
            db.commit()
    return ids


@pytest.fixture
def east_eight(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        yield
    finally:
        monkeypatch.undo()
        time.tzset()


def test_次日早上八点前报的甲类卡_进迟报清单(client, admin, cases, east_eight):
    listed = {r["case_id"]: r for r in client.get("/api/infectious/late-reports", headers=admin).json()}
    early, after_eight = cases
    assert early in listed, "东八区次日 07:30 报的鼠疫卡不在迟报清单里"   # 修前：报告日按 UTC 算成发病当天
    assert listed[early]["days_late"] == listed[after_eight]["days_late"] == 1


def test_报告卡导出与迟报清单同一个判定(client, admin, cases, east_eight):
    resp = client.get("/api/infectious/cases/export.csv", headers=admin)
    assert resp.status_code == 200, resp.text
    rows = {int(r["卡片编号"]): r for r in csv.DictReader(io.StringIO(resp.text.lstrip("﻿")))}
    early, after_eight = cases
    assert (rows[early]["迟报天数"], rows[early]["及时性"]) == ("1", "迟报")   # 修前 0 / 及时
    assert (rows[after_eight]["迟报天数"], rows[after_eight]["及时性"]) == ("1", "迟报")
