"""复诊计划看板与随访看板没筛选时，未做完的排在最前（P2-783，第二十批「同一个数，多处口径」扫描 M1-6）。

两块看板默认不筛状态、按计划日升序取最早一页：攒过 50 / 30 条之后首屏全是早已复诊 / 办完的，今天到期的看不到，团队端却报
「到期复诊 / 到期随访 N」。实测：团队工作台到期复诊 1、移动端今日复诊 1；看板默认 50 行（总 51），计划日 2026-03-13 到
2026-08-07、全是已复诊，今天那条不在。修法同 P2-782：没筛选时逾期 / 待复诊（已超期 / 待随访）单独取一遍、排在最前；
筛了状态或「只看逾期」的照筛的来。
"""
import os
from datetime import date, timedelta

import pytest

from app.database import SessionLocal

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
B = "/api/spd"


def _function(name: str) -> str:
    with open(os.path.join(STATIC, "pages-spd.js"), encoding="utf-8") as fh:
        source = fh.read()
    start = source.index(f"async function {name}()")
    end = source.find("\nasync function ", start + 1)
    return source[start:end if end != -1 else len(source)]


def test_随访看板没筛选时未做完的排在最前():
    body = _function("renderSpdFollowup")
    draw = body[body.index("const drawRecords = async (query) => {"):]
    draw = draw[:draw.index("$(\"#spd-fu-list\")")]
    assert "query && Object.keys(query).length ?" in draw   # 筛了的照筛的来
    for fetch in ('api("/api/spd/followup-records?status=overdue&limit=200")',
                  'api("/api/spd/followup-records?status=planned&limit=200")'):
        assert fetch in draw, fetch   # 修前只取按计划日升序的最早一页
    assert "actionableFirst(...await Promise.all(" in draw


def test_复诊看板默认与不筛选的查询都是未做完的排在最前():
    body = _function("renderSpdManager")
    assert ('const openFirstRevisits = async () => actionableFirst(...await Promise.all([api("/api/spd/revisits?limit=50"),'
            in body)
    for fetch in ('api("/api/spd/revisits?status=overdue&limit=200")', 'api("/api/spd/revisits?status=planned&limit=200")'):
        assert fetch in body, fetch
    assert body.count("openFirstRevisits()") == 2   # 首屏与「全部状态」的筛选各一处


@pytest.fixture(scope="module")
def today_revisit(client, admin):
    from app.spd.models import SpdRevisit

    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2783 患者", "id_card": "330102195001012783"}).json()["id"]
    start = date(2026, 3, 13)
    with SessionLocal() as db:
        db.add_all([SpdRevisit(patient_id=patient, program_code="hypertension", status="done",
                               plan_date=(start + timedelta(days=3 * i)).isoformat()) for i in range(50)])
        today = SpdRevisit(patient_id=patient, program_code="hypertension", status="planned",
                           plan_date=date.today().isoformat())
        db.add(today)
        db.commit()
        return today.id


def test_最早一页里没有今天的复诊_按状态取得到(client, admin, today_revisit):
    earliest = [r["id"] for r in client.get(f"{B}/revisits", headers=admin, params={"limit": 50}).json()]
    assert today_revisit not in earliest   # 判据自证：默认一页全是早已复诊的
    planned = client.get(f"{B}/revisits", headers=admin, params={"status": "planned", "limit": 200}).json()
    assert today_revisit in [r["id"] for r in planned]
