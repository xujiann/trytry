"""绿道节点时间一个带时区偏移、一个不带：比时序时 Python 抛 TypeError，记节点整个 500（P2-263）。

记节点的时间校验收任意 ISO 写法：页面上手填的本地时间（`2026-09-20 08:00`，不带时区）与对接系统送来的带偏移时间
（`2026-09-21T08:05:00+08:00`）都过校验。记节点时要与已记的节点比时序（不得「先救治后发病」），带偏移的与不带的
直接比较，`TypeError: can't compare offset-naive and offset-aware datetimes`——500。修法：带偏移的先换算成本地钟点再比。
"""
import pytest


@pytest.fixture()
def case_id(client, admin):
    resp = client.post("/api/emergency/cases", headers=admin,
                       json={"location": "P2263 事发地", "symptom": "胸痛", "channel_type": "chest_pain"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _milestone(client, admin, case_id, milestone, occurred_at):
    return client.post(f"/api/emergency/cases/{case_id}/milestones", headers=admin,
                       json={"milestone": milestone, "occurred_at": occurred_at})


def test_带偏移的与不带的混着记_照常比时序(client, admin, case_id):
    assert _milestone(client, admin, case_id, "onset", "2026-09-20 08:00").status_code == 201
    # 次日的带偏移时间：换算到任何时区都在前一天 08:00 之后
    later = _milestone(client, admin, case_id, "call", "2026-09-21T08:05:00+08:00")
    assert later.status_code == 201, later.text   # 修前 500：TypeError
    earlier = _milestone(client, admin, case_id, "depart", "2026-09-19T08:00:00+08:00")
    assert earlier.status_code == 422 and "时序矛盾" in earlier.json()["detail"], earlier.text


def test_先记带偏移的_再记不带的_同样比得了(client, admin, case_id):
    assert _milestone(client, admin, case_id, "call", "2026-09-21T08:05:00+08:00").status_code == 201
    resp = _milestone(client, admin, case_id, "treatment", "2026-09-19 09:00")
    assert resp.status_code == 422 and "时序矛盾" in resp.json()["detail"], resp.text   # 修前 500
