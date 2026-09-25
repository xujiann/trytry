"""「按患者特征自动匹配」随访方案：出院场景不认回溯天数，同一次扫描里同一位患者命中几次就排几份（P1-134）。

随访页的自动匹配表单写着「回溯天数」（默认 7）。门诊 / 术后 / 体检三个场景按它取近 N 天的就诊；默认的出院场景却不看它，
取的是本机构最近建档的 200 条已出院住院记录——两年前出院的也在内。随访时间点是加在出院日上的，那些人排出来的随访
计划日期早已过去，超期扫描一过就全是「已超期」：随访完成率、超期督办与考核一并被拉低，基层看到的是一批没法做的随访。

同一次扫描里的查重（「同一患者同一方案只派生一次」）是先查库再 add，而会话不自动 flush（`autoflush=False`）：同一位
患者近几天有两次就诊、或一周内出院两次，第二次命中时查不到第一次刚 add 的计划，再排一整份——模块文档说的「同一个
病人两份随访计划，基层只会当成系统出错」。

修法：出院场景与其余场景同一口径，按出院时间取回溯窗口内的住院记录；本次扫描已派生过的「患者 + 方案」记在手上，不再派。
（隔几个月再次住院的是否该再排一份，不在本条：查重的键是「患者 + 方案」而不是「这一次住院」，见待裁定清单。）
"""
from datetime import timedelta

import pytest

from app.clock import now_naive

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.models import User

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P134 随访医院", "org_type": "township", "level": "township"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P134 病区"}).json()["id"]
    for scene, keyword in (("inpatient", "P134出院病"), ("outpatient", "P134门诊病")):
        resp = client.post(f"{B}/followup-rules", headers=admin, json={
            "code": f"P134_{scene}", "name": f"P134 {scene} 方案", "scene": scene,
            "diagnosis_keywords": [keyword], "points": [7, 30]})
        assert resp.status_code == 201, resp.text
    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
    return {"org": org, "ward": ward, "admin_id": admin_id, "n": 0}


def _patient(client, admin, world):
    world["n"] += 1
    return client.post("/api/patients", headers=admin, json={
        "name": f"P134 患者{world['n']}", "id_card": f"33012719650505{world['n']:04d}"}).json()["id"]


def _discharged(client, admin, world, patient_id, days_ago):
    """一条 days_ago 天前出院、诊断命中出院方案的住院记录；返回出院日。"""
    from app.database import SessionLocal
    from app.models import Admission

    bed = client.post("/api/inpatient/beds", headers=admin, json={
        "ward_id": world["ward"], "bed_no": f"P134-{patient_id}-{days_ago}"}).json()["id"]
    discharged_at = now_naive() - timedelta(days=days_ago)
    with SessionLocal() as db:
        db.add(Admission(patient_id=patient_id, org_id=world["org"], ward_id=world["ward"], bed_id=bed,
                         diagnosis_name="P134出院病", status="discharged", created_by=world["admin_id"],
                         admitted_at=discharged_at - timedelta(days=5), discharged_at=discharged_at))
        db.commit()
    return discharged_at.date()


def _planned(patient_id):
    from app.database import SessionLocal
    from app.spd.models import SpdFollowupRecord

    with SessionLocal() as db:
        return sorted(p for (p,) in db.query(SpdFollowupRecord.planned_at)
                      .filter(SpdFollowupRecord.patient_id == patient_id))


def test_出院场景按回溯天数取_早已出院的不排过期随访(client, admin, world):
    old, recent = _patient(client, admin, world), _patient(client, admin, world)
    _discharged(client, admin, world, old, days_ago=200)
    day = _discharged(client, admin, world, recent, days_ago=2)
    resp = client.post(f"{B}/followup-plans/auto-match", headers=admin, json={
        "scene": "inpatient", "org_id": world["org"], "days": 7})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"scanned": 1, "matched": 1, "created": 2}, resp.text   # 修前 2 / 2 / 4
    assert _planned(old) == []   # 修前两条，计划日期在半年多以前，一排出来就超期
    assert _planned(recent) == [(day + timedelta(days=7)).isoformat(), (day + timedelta(days=30)).isoformat()]


def test_一周内两次出院_同一次扫描只排一份_按最近一次出院(client, admin, world):
    patient = _patient(client, admin, world)
    _discharged(client, admin, world, patient, days_ago=6)
    day = _discharged(client, admin, world, patient, days_ago=1)
    resp = client.post(f"{B}/followup-plans/auto-match", headers=admin, json={
        "scene": "inpatient", "org_id": world["org"], "days": 7})
    assert resp.status_code == 200 and resp.json()["created"] == 2, resp.text   # 修前 4
    assert _planned(patient) == [(day + timedelta(days=7)).isoformat(), (day + timedelta(days=30)).isoformat()]


def test_近几天两次就诊_同一次扫描只排一份(client, admin, world):
    patient = _patient(client, admin, world)
    for _ in range(2):
        resp = client.post("/api/encounters", headers=admin, json={
            "patient_id": patient, "org_id": world["org"], "encounter_type": "outpatient",
            "diagnosis_name": "P134门诊病"})
        assert resp.status_code == 201, resp.text
    resp = client.post(f"{B}/followup-plans/auto-match", headers=admin, json={
        "scene": "outpatient", "org_id": world["org"], "days": 7})
    assert resp.status_code == 200 and resp.json()["created"] == 2, resp.text   # 修前 4
    assert len(_planned(patient)) == 2
    # 再扫一遍照旧不重复（跨次的查重本来就有）
    again = client.post(f"{B}/followup-plans/auto-match", headers=admin, json={
        "scene": "outpatient", "org_id": world["org"], "days": 7})
    assert again.status_code == 200 and again.json()["created"] == 0, again.text
