"""共享中心排班不带日期时给今天及以后的排班（P2-208）。

原先按日期升序取前 200 条：排班一多只剩最早那 200 条历史，今天谁值班、往后怎么排在协同办公页上都看不到
（页面不带日期调用）。手术排班表的同一形状 P2-155 已修；查某一天的照旧等值查。
"""
from datetime import date, timedelta

import pytest

from app.database import SessionLocal
from app.models import DutyRoster


@pytest.fixture(scope="module")
def seeded(client):
    today = date.today()
    with SessionLocal() as db:
        for back in range(1, 241):   # 过去 240 天每天一班
            db.add(DutyRoster(center_type="imaging", duty_date=(today - timedelta(days=back)).isoformat(),
                              doctor_name=f"P2208 历史{back}"))
        db.add(DutyRoster(center_type="imaging", duty_date=today.isoformat(), doctor_name="P2208 今天"))
        db.add(DutyRoster(center_type="imaging", duty_date=(today + timedelta(days=1)).isoformat(), doctor_name="P2208 明天"))
        db.commit()
    return today


def test_不带日期给今天及以后(client, admin, seeded):
    rows = client.get("/api/mgmt/rosters", headers=admin).json()
    assert [r["doctor_name"] for r in rows] == ["P2208 今天", "P2208 明天"]      # 修前是最早的 200 条历史


def test_查某一天照旧等值查(client, admin, seeded):
    day = (seeded - timedelta(days=100)).isoformat()
    rows = client.get(f"/api/mgmt/rosters?duty_date={day}", headers=admin).json()
    assert [r["doctor_name"] for r in rows] == ["P2208 历史100"]
