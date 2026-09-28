"""「历史随访」与居民时间线按执行日期取最近的几次，不按随访记录编号（P2-691，第十七批「最近 / 最新」扫描 U4-6）。

随访记录是建计划时成批编号的（按方案一次排出 7 / 30 / 90 / 180 / 365 天），执行日期是办完那天才填。原先两处都按编号
倒序截：医护端「随访前置资料 → 历史随访」取 10 条、居民端档案时间线取 20 条——一年前建的计划里昨天才做的年度随访
编号最小，被后建计划的月度随访挤出去，医护与居民都看不到最近这次随访。
"""
from datetime import date

import pytest

from app.database import SessionLocal
from app.spd.models import SpdFollowupRecord

PATIENT = {"name": "P2691 随访居民", "id_card": "330106195803030610", "gender": "男",
           "birth_date": "1958-03-03", "phone": "13900006910"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2691 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    resp = client.post("/api/patients", headers=admin, json=PATIENT)
    assert resp.status_code in (200, 201), resp.text
    pid = resp.json()["id"]
    with SessionLocal() as db:
        def record(**kw):
            row = SpdFollowupRecord(patient_id=pid, org_id=org, program_code="hypertension", scene="chronic", **kw)
            db.add(row)
            db.flush()
            return row.id
        # 去年建的计划：年度随访（编号最小）昨天才执行；另一条还没到期，当本次随访的锚点
        annual = record(planned_at="2026-09-20", executed_at="2026-09-27", status="done", result="年度随访：血压控制平稳")
        anchor = record(planned_at="2026-12-20", status="planned")
        # 后建的计划：21 次月度随访，编号都比年度随访大，执行日期都更早
        for i in range(21):
            year, month = divmod(2024 * 12 + 11 + i, 12)   # 2024-12 起的 21 个月
            record(planned_at=date(year, month + 1, 15).isoformat(), executed_at=date(year, month + 1, 15).isoformat(),
                   status="done", result=f"月度随访 {i + 1}")
        db.commit()
    return {"annual": annual, "anchor": anchor}


def test_医护端历史随访_最近执行的那次在最前(client, admin, world):
    resp = client.get(f"/api/spd/followup-records/{world['anchor']}/context", headers=admin)
    assert resp.status_code == 200, resp.text
    history = resp.json()["history"]
    assert len(history) == 10
    assert history[0]["id"] == world["annual"]   # 修前：按编号倒序，年度随访被 10 条月度随访挤掉
    dates = [h["executed_at"] for h in history]
    assert dates == sorted(dates, reverse=True)


def test_居民端时间线_最近执行的那次在里面(client, world):
    code = client.post("/api/portal/auth/sms/code", json={"phone": PATIENT["phone"]}).json()["debug_code"]
    login = client.post("/api/portal/auth/sms/login", json={"phone": PATIENT["phone"], "code": code})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    client.post("/api/portal/auth/realname", headers=headers,
                json={"name": PATIENT["name"], "id_card": PATIENT["id_card"]})
    resp = client.get("/api/portal/spd/archive", headers=headers)
    assert resp.status_code == 200, resp.text
    followups = [t for t in resp.json()["timeline"] if t["kind"] == "followup"]
    assert followups[0]["at"] == "2026-09-27" and followups[0]["detail"] == "年度随访：血压控制平稳"   # 修前不在这 20 条里
