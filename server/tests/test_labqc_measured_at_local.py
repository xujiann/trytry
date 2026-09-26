"""室内质控测定时间留空时按本地时刻记，与页面上手填的本地时间同一把尺子（P2-171）。

测定时间是给人看的字符串（「YYYY-MM-DD HH:MM」，清单与失控处理都照它看）。页面上手填用的是 datetime-local
（本地时间），留空时接口原先填 UTC：东八区早上 7 点半留空录的点记成前一天 23:30，同一张清单里手填的与留空的
差着 8 小时。`clock.now_local` 的说明写它正是给「记录时间默认值」用的，门急诊文书的记录时间缺省早就这么取。
"""
import os
import time
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def shanghai():
    """把进程时区拨到东八区，用完拨回去——本地与 UTC 差 8 小时，取错了一眼看得出。"""
    saved = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


def test_留空的测定时间按本地时刻记(client, admin, shanghai):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2171 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    lot = client.post("/api/labqc/lots", headers=admin, json={
        "org_id": org, "item_code": "K", "item_name": "钾", "lot_no": "P2171", "target_value": 4.0, "sd": 0.2})
    assert lot.status_code == 201, lot.text
    created = client.post(f"/api/labqc/lots/{lot.json()['id']}/measurements", headers=admin, json={"value": 4.1})
    assert created.status_code == 201, created.text
    recorded = datetime.strptime(created.json()["measured_at"], "%Y-%m-%d %H:%M")
    local_now = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
    assert abs(recorded - local_now) < timedelta(minutes=5), recorded   # 修前差 8 小时（取的是 UTC）

    # 手填的原样落库，不做换算
    typed = client.post(f"/api/labqc/lots/{lot.json()['id']}/measurements", headers=admin,
                        json={"value": 4.0, "measured_at": "2026-09-26 07:30"})
    assert typed.json()["measured_at"] == "2026-09-26 07:30"
