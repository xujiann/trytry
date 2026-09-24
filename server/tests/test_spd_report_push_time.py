"""报告推送时点只收零补齐的 HH:MM：「8:00」「24:00」原先照存，调度按字符串比较永远跳过、也不报错（P2-53）。

2026-09-24 实测（修前代码，开发库）：三个推送任务的 push_time 分别是 8:00 / 08:00 / 24:00，把时钟冻结在 09:00
与 23:59 各跑一轮调度，只有 08:00 那条生成了报告，另两条 `last_run_at` 一直为空。调度的判定是
`现在.strftime("%H:%M") < push_time` 则本轮跳过——「8:00」「24:00」在字符串比较里比一天中任何时刻都大。
界面上推送时间是自由文本框，填「8:00」再自然不过。

修法：建任务与改任务的 push_time 同一个 `PUSH_TIME_PATTERN`（`app/spd/routers/followup.py`），只收 ASCII 数字。
"""
from datetime import datetime, time

import pytest

from app import clock

B = "/api/spd"


@pytest.fixture(scope="module")
def template(client, admin):
    resp = client.post(f"{B}/report-templates", headers=admin, json={
        "code": "P253-T", "name": "推送时点", "period": "daily", "sections": [{"key": "summary"}]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.parametrize("push_time", ["8:00", "24:00", "08:60", "０８:００", "08:00:00", "8点"])
def test_建推送任务_时点不是零补齐的HHMM是422(client, admin, template, push_time):
    resp = client.post(f"{B}/report-tasks", headers=admin,
                       json={"template_id": template, "name": "时点校验", "push_time": push_time})
    assert resp.status_code == 422, (push_time, resp.text)


def test_改推送任务_时点同一句校验(client, admin, template):
    task = client.post(f"{B}/report-tasks", headers=admin,
                       json={"template_id": template, "name": "改时点", "push_time": "08:00"})
    assert task.status_code == 201, task.text
    bad = client.patch(f"{B}/report-tasks/{task.json()['id']}", headers=admin, json={"push_time": "8:00"})
    assert bad.status_code == 422, bad.text
    ok = client.patch(f"{B}/report-tasks/{task.json()['id']}", headers=admin, json={"push_time": "09:30"})
    assert ok.status_code == 200 and ok.json()["push_time"] == "09:30", ok.text


def test_合法时点到点即推(client, admin, template, monkeypatch):
    from app.database import SessionLocal
    from app.spd.jobs import spd_report_push

    task = client.post(f"{B}/report-tasks", headers=admin,
                       json={"template_id": template, "name": "八点推送", "push_time": "08:00"})
    assert task.status_code == 201, task.text
    monkeypatch.setattr(clock, "now_naive", lambda: datetime.combine(clock.today(), time(9, 0)))
    with SessionLocal() as db:
        spd_report_push(db)
        db.commit()
    rows = client.get(f"{B}/report-tasks", headers=admin).json()
    assert [t["last_run_at"] != "" for t in rows if t["id"] == task.json()["id"]] == [True]
