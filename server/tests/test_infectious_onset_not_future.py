"""传染病报卡的发病日期能填未来：平台自己的质控规则 QC015 事后才点名，写入时照收（P2-454）。

QC015「传染病报告发病日期不得晚于当日」（严重级）是平台内置的质控规则，可 `POST /api/infectious/cases` 写入时不查：
2099 年发病的鼠疫报卡 201，报卡上的迟报天数算成 -26394、永不进迟报清单；把月份敲错成下个月的一张手足口病报卡落在
多点触发预警的窗口之外，同病种凑够 5 例的预警就少一例、整条不出。修后：发病日期晚于今天即 422；当天发病照收。
"""
from datetime import date

import pytest

from conftest import freeze_business_date


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P2454 发热门诊医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]


def _report(client, admin, org, onset):
    return client.post("/api/infectious/cases", headers=admin, json={
        "org_id": org, "disease_code": "A20", "disease_name": "鼠疫", "onset_date": onset})


def test_发病日期晚于今天_422(client, admin, org):
    with freeze_business_date(date(2026, 9, 27)):
        tomorrow = _report(client, admin, org, "2026-09-28")
        far = _report(client, admin, org, "2099-01-01")
    assert tomorrow.status_code == 422, tomorrow.text   # 修前 201
    assert tomorrow.json() == {"detail": "发病日期（2026-09-28）不得晚于今天"}
    assert far.status_code == 422, far.text             # 修前 201，报卡迟报天数 -26394、永不进迟报清单


def test_当天与之前发病照收(client, admin, org):
    with freeze_business_date(date(2026, 9, 27)):
        today = _report(client, admin, org, "2026-09-27")
        earlier = _report(client, admin, org, "2026-09-20")
    assert (today.status_code, earlier.status_code) == (201, 201), (today.text, earlier.text)
