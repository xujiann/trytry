"""居民端慢专病「就诊与管理时间线」先各源截断再合并：比列出的随访更近的就诊被静默丢掉（P2-1183，第三十四批扫描 L4-9）。

`portal.archive` 就诊取最近 30 次、随访取最近 20 次，合并按时间排序后再截 50。就诊多的居民（透析、换药）第 31 次以后的就诊
没进来，紧接着第 30 次就诊的是更早的随访——时间线中间静默断档，没有任何截断提示。平台居民端的两个聚合接口写明「条数上限
是合并之后才截的」（`/me/referrals/all`、`/me/enrollments/all`）。

修法：两源各取 50（合并后的上限）再合并，按原来的排序键排序后截 50。
"""
from datetime import date, datetime, time, timedelta

import pytest

from app.config import settings
from app.database import SessionLocal

BASE = date(2026, 9, 1)


def _day(k: int) -> date:
    return BASE - timedelta(days=k)


def _resident(client, admin, tag: str, id_card: str, phone: str, encounter_days, followup_days):
    """现造一位居民：k 天前各一次就诊（每天两点）、k 天前各一次已办结的随访；返回居民端请求头。"""
    from app.models import Encounter, Patient, ResidentAccount
    from app.spd.models import SpdFollowupRecord

    org = client.post("/api/organizations", headers=admin, json={
        "name": f"P21183 {tag}透析中心", "org_type": "township", "level": "township"}).json()["id"]
    with SessionLocal() as db:
        me = Patient(ehc_no=f"EHC-P21183-{tag}", name=f"P21183 {tag}", id_card=id_card, gender="男",
                     birth_date="1963-03-03", phone=phone)
        db.add(me)
        db.flush()
        db.add(ResidentAccount(phone=phone, patient_id=me.id, nickname=tag, status="active"))
        for k in encounter_days:
            db.add(Encounter(patient_id=me.id, org_id=org, diagnosis_name=f"血液透析（{k} 天前）",
                             created_at=datetime.combine(_day(k), time(2, 0))))
        for k in followup_days:
            db.add(SpdFollowupRecord(patient_id=me.id, program_code="ckd", scene="outpatient", planned_at=_day(k).isoformat(),
                                     executed_at=_day(k).isoformat(), status="done", result=f"{k} 天前的随访"))
        db.commit()
    old = settings.sms_debug_echo
    settings.sms_debug_echo = True
    try:
        code = client.post("/api/portal/auth/sms/code", json={"phone": phone, "purpose": "login"}).json()["debug_code"]
        token = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code}).json()["access_token"]
    finally:
        settings.sms_debug_echo = old
    return {"Authorization": f"Bearer {token}"}


def _timeline(client, headers):
    resp = client.get("/api/portal/spd/archive", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["timeline"]


def test_比列出的随访更近的就诊不被丢掉(client, admin):
    headers = _resident(client, admin, "甲", "330102196303031183", "13912201183",
                        encounter_days=range(1, 41), followup_days=(35, 100, 200))
    timeline = _timeline(client, headers)
    assert len(timeline) == 43   # 修前 33：就诊只取了 30 次
    shown = {t["title"] for t in timeline if t["kind"] == "encounter"}
    assert shown == {f"血液透析（{k} 天前）" for k in range(1, 41)}   # 修前 31～40 天前的 10 次不在
    assert [t["at"] for t in timeline] == sorted((t["at"] for t in timeline), reverse=True)   # 排序键照旧
    # 40 次就诊全排在 100 天前那次随访之前：不再出现「第 30 次就诊后面直接接 100 天前的随访」的断档
    assert [t["detail"] for t in timeline[-2:]] == ["100 天前的随访", "200 天前的随访"]


def test_两源合计超过上限_合并排序后截最近的50条(client, admin):
    headers = _resident(client, admin, "乙", "330102196303041183", "13912211183",
                        encounter_days=range(1, 61), followup_days=(10, 20))
    timeline = _timeline(client, headers)
    assert len(timeline) == 50   # 修前 32：就诊 30 次 + 随访 2 次
    assert {t["detail"] for t in timeline if t["kind"] == "followup"} == {"10 天前的随访", "20 天前的随访"}
    shown = {t["title"] for t in timeline if t["kind"] == "encounter"}
    assert shown == {f"血液透析（{k} 天前）" for k in range(1, 49)}   # 最近的 48 次；更早的按时间截掉
