"""签约建档与手工改目标池状态不跑病种排除规则：16 岁居民直接签进「成人高血压管理」（P2-1573，第四十六批扫描 AJ3-2）。

`service.exclusion_problem`（P2-935）只挡在自查、申请、受理、复核四处。建档是最后一道门却不看排除规则——修前实测：16 岁、
高血压确诊的居民筛查结论「排除」，池里那一行原因「未成年人不纳入成人高血压管理」，复核确认 409，可改池状态为目标 200（原因照旧）、
签约建档 201（池行还被改成已纳管）；另一位 15 岁、从没筛查过的居民直接建档同样 201。

修法：建档、把池行改成目标 / 疑似都过 `exclusion_problem`，命中 409，文案与受理同一句开头（「按病种规则不纳入（…）」）；改成排除
等其它状态不判。成年人不受影响。
"""
from datetime import date

import pytest

from app.database import SessionLocal
from app.spd.models import SpdCandidate, SpdEnrollment
from conftest import business_today

B = "/api/spd"
HIGH = {"family": "是", "salt": "是", "overweight": "是", "smoke": "否", "drink": "否", "symptom": "是"}
RULE = "按病种规则不纳入（未成年人不纳入成人高血压管理）"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21573 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    return {"org": org, "n": 0}


def _person(client, admin, world, *, minor, screened):
    """一位高血压确诊的居民（未成年的满 16 岁、不满 18 岁，出生日期现取）；`screened` 的再做一次医护筛查进池。"""
    world["n"] += 1
    birth = date(business_today().year - 16, 1, 1).isoformat() if minor else "1970-01-01"
    made = client.post("/api/patients", headers=admin, json={
        "name": f"P21573 居民{world['n']}", "id_card": f"33010620100101{1573 + world['n']:04d}", "gender": "男",
        "birth_date": birth})
    assert made.status_code in (200, 201), made.text
    patient = made.json()["id"]
    visit = client.post("/api/encounters", headers=admin, json={
        "patient_id": patient, "org_id": world["org"], "diagnosis_code": "I10", "diagnosis_name": "原发性高血压"})
    assert visit.status_code in (200, 201), visit.text
    if screened:   # 医护筛查：排除规则先跑，未成年的池行是「排除」
        got = client.post(f"{B}/screenings", headers=admin, json={
            "patient_id": patient, "program_code": "hypertension", "org_id": world["org"],
            "scale_code": "scr_hypertension", "answers": HIGH})
        assert got.status_code == 201 and got.json()["result"] == ("excluded" if minor else "suspect"), got.text
    return patient


def _pool(patient_id):
    with SessionLocal() as db:
        row = db.query(SpdCandidate).filter_by(patient_id=patient_id, program_code="hypertension").first()
        return None if row is None else {"id": row.id, "status": row.status, "reason": row.reason}


def _enrollments(patient_id):
    with SessionLocal() as db:
        return db.query(SpdEnrollment).filter_by(patient_id=patient_id, program_code="hypertension").count()


def _enroll(client, admin, world, patient_id):
    return client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient_id, "program_code": "hypertension", "org_id": world["org"]})


def test_未成年人签约建档409_池行不动(client, admin, world):
    teen = _person(client, admin, world, minor=True, screened=True)
    before = _pool(teen)
    assert before["status"] == "excluded" and "未成年人不纳入" in before["reason"]
    resp = _enroll(client, admin, world, teen)
    assert resp.status_code == 409, resp.text   # 修前 201
    assert resp.json() == {"detail": f"{RULE}，不能签约建档"}
    assert _pool(teen) == before   # 修前池行改成已纳管
    assert _enrollments(teen) == 0


@pytest.mark.parametrize("status,label", [("target", "目标人群"), ("suspect", "疑似人群")])
def test_排除的池行改成目标或疑似_409(client, admin, world, status, label):
    teen = _person(client, admin, world, minor=True, screened=True)
    before = _pool(teen)
    resp = client.post(f"{B}/candidates/{before['id']}/status", headers=admin, json={"status": status})
    assert resp.status_code == 409, resp.text   # 修前 200，原因栏照写「未成年人不纳入…」
    assert resp.json() == {"detail": f"{RULE}，不能改为{label}"}
    assert _pool(teen) == before


def test_改成排除不判排除规则(client, admin, world):
    teen = _person(client, admin, world, minor=True, screened=True)
    resp = client.post(f"{B}/candidates/{_pool(teen)['id']}/status", headers=admin,
                       json={"status": "excluded", "reason": "家属拒绝"})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["status"], resp.json()["reason"]) == ("excluded", "家属拒绝")


def test_没筛查过的未成年人直接建档409_池里不添行(client, admin, world):
    teen = _person(client, admin, world, minor=True, screened=False)
    resp = _enroll(client, admin, world, teen)
    assert resp.status_code == 409, resp.text   # 修前 201
    assert resp.json() == {"detail": f"{RULE}，不能签约建档"}
    assert _pool(teen) is None and _enrollments(teen) == 0


def test_成年人改状态与建档照常(client, admin, world):
    adult = _person(client, admin, world, minor=False, screened=True)
    candidate = _pool(adult)["id"]
    for status in ("target", "suspect", "target"):
        resp = client.post(f"{B}/candidates/{candidate}/status", headers=admin, json={"status": status})
        assert resp.status_code == 200 and resp.json()["status"] == status, resp.text
    enrolled = _enroll(client, admin, world, adult)
    assert enrolled.status_code == 201, enrolled.text
    assert _pool(adult)["status"] == "enrolled" and _enrollments(adult) == 1


def test_成年人没筛查过直接建档照常(client, admin, world):
    adult = _person(client, admin, world, minor=False, screened=False)
    enrolled = _enroll(client, admin, world, adult)
    assert enrolled.status_code == 201, enrolled.text
    assert _pool(adult)["status"] == "enrolled"   # 直接建档在池里记一行已纳管（P2-359），照旧
