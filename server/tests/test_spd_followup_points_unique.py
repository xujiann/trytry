"""随访方案的时间点不许重复，存量里重复的排随访时去重（P2-719，第十八批「数值入参的符号与业务上下界」扫描 V3-8）。

时间点（`points`）只查非空与 0～3650 天。写成 [7, 7, 30]：「按方案生成随访」与「按患者特征自动匹配」逐个时间点各排
一条，第 7 天排两条——两条都要执行，都进完成率与超期数，基层只会当成系统出错。页面上时间点是逗号分隔录入，多敲一个
「7」就进去了。

修法：建方案与改方案同一句，时间点重复 422；写入口查重复之前存下的方案，排随访时按时间点去重。
"""
import pytest

from app.database import SessionLocal
from app.spd.models import SpdFollowupRecord, SpdFollowupRule

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2719 随访卫生院", "org_type": "township", "level": "township"}).json()["id"]
    return {"org": org, "n": 0}


def _patient(client, admin, world):
    world["n"] += 1
    return client.post("/api/patients", headers=admin, json={
        "name": f"P2719 患者{world['n']}", "id_card": f"33012719660606{world['n']:04d}"}).json()["id"]


def _rule(client, admin, code, points, scene="outpatient", keyword=""):
    return client.post(f"{B}/followup-rules", headers=admin, json={
        "code": code, "name": f"{code} 方案", "scene": scene, "points": points,
        "diagnosis_keywords": [keyword] if keyword else []})


def _legacy_duplicate(rule_id):
    with SessionLocal() as db:   # 写入口查重复之前存下的
        db.get(SpdFollowupRule, rule_id).points = [7, 7, 30]
        db.commit()


def _planned(patient_id):
    with SessionLocal() as db:
        return sorted(p for (p,) in db.query(SpdFollowupRecord.planned_at).filter(SpdFollowupRecord.patient_id == patient_id))


def test_建方案与改方案_时间点重复422(client, admin):
    resp = _rule(client, admin, "P2719_DUP", [7, 7, 30])
    assert resp.status_code == 422 and "7 天" in resp.json()["detail"], resp.text   # 修前 201
    ok = _rule(client, admin, "P2719_OK", [7, 30])
    assert ok.status_code == 201, ok.text
    patched = client.patch(f"{B}/followup-rules/{ok.json()['id']}", headers=admin, json={"points": [30, 30]})
    assert patched.status_code == 422, patched.text   # 修前 200
    with SessionLocal() as db:
        assert db.get(SpdFollowupRule, ok.json()["id"]).points == [7, 30]


def test_存量重复时间点_按方案生成随访去重(client, admin, world):
    rule = _rule(client, admin, "P2719_GEN", [7, 30]).json()["id"]
    _legacy_duplicate(rule)
    patient = _patient(client, admin, world)
    resp = client.post(f"{B}/followup-plans", headers=admin, json={
        "patient_id": patient, "rule_id": rule, "org_id": world["org"], "base_date": "2026-09-01"})
    assert resp.status_code == 201 and resp.json()["created"] == 2, resp.text   # 修前 3
    assert _planned(patient) == ["2026-09-08", "2026-10-01"]


def test_存量重复时间点_自动匹配也去重(client, admin, world):
    rule = _rule(client, admin, "P2719_AUTO", [7, 30], keyword="P2719门诊病").json()["id"]
    _legacy_duplicate(rule)
    patient = _patient(client, admin, world)
    visit = client.post("/api/encounters", headers=admin, json={
        "patient_id": patient, "org_id": world["org"], "encounter_type": "outpatient", "diagnosis_name": "P2719门诊病"})
    assert visit.status_code == 201, visit.text
    resp = client.post(f"{B}/followup-plans/auto-match", headers=admin, json={
        "scene": "outpatient", "org_id": world["org"], "days": 7})
    assert resp.status_code == 200 and resp.json()["created"] == 2, resp.text   # 修前 3
    assert len(_planned(patient)) == 2


def test_存量重复时间点_出院即派生也去重(client, admin, world):
    from app.spd.subscribers import on_admission_discharged

    rule = _rule(client, admin, "P2719_DISCHARGE", [7, 30], scene="inpatient", keyword="P2719出院病").json()["id"]
    _legacy_duplicate(rule)
    patient = _patient(client, admin, world)
    with SessionLocal() as db:
        on_admission_discharged(db, {"patient_id": patient, "diagnosis_name": "P2719出院病", "discharged_on": "2026-09-01",
                                     "org_id": world["org"]})
        db.commit()
    assert _planned(patient) == ["2026-09-08", "2026-10-01"]   # 修前第 7 天两条
