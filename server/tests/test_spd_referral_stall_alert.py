"""转诊「超时未推进」从最近一次推进起算，不从建单起算（P2-140）。

转诊页的「超时未推进 N」卡片与医生移动端工作台的超时转诊数，接口说明写的都是「超过 N 小时未推进的在途单」；
实现却按建单时间判：
- 三天前发起、刚被审核通过的单子照报超时——卫生院刚办完，预警上还挂着它；
- 真正卡在一个环节上的单子，列表排序也不按卡了多久：一张很早发起、中途推进过一格又卡住的单子，
  排在一张卡在发起环节更久的单子前面。

修法：两处共用 `service.referral_last_moved_at`（最后一条环节轨迹的时间，存量无轨迹的退回建单时间）。
"""
from datetime import timedelta

import pytest

B = "/api/spd"


def _login(client, username):
    resp = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _backdate(case_id, *, created_hours_ago, steps_hours_ago):
    """把单子的建单时间与环节轨迹（按先后）挪到若干小时前。"""
    from app.clock import now_naive
    from app.database import SessionLocal
    from app.spd.models import SpdReferralCase, SpdReferralStep

    now = now_naive()
    with SessionLocal() as db:
        db.get(SpdReferralCase, case_id).created_at = now - timedelta(hours=created_hours_ago)
        steps = db.query(SpdReferralStep).filter(SpdReferralStep.case_id == case_id).order_by(SpdReferralStep.id).all()
        assert len(steps) == len(steps_hours_ago)
        for step, hours in zip(steps, steps_hours_ago):
            step.created_at = now - timedelta(hours=hours)
        db.commit()


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2140 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p2140_doc", "password": "passw0rd1", "full_name": "P2140 医生", "role": "doctor", "org_id": org})
    assert created.status_code in (200, 201), created.text
    doctor = _login(client, "p2140_doc")
    patients = [client.post("/api/patients", headers=admin, json={
        "name": f"P2140 患者{i}", "id_card": f"33010619690909{i:04d}", "gender": "女"}).json()["id"] for i in range(3)]
    with SessionLocal() as db:
        db.add_all([SpdEnrollment(patient_id=p, program_code="hypertension", org_id=org, status="active")
                    for p in patients])
        db.commit()

    def refer(patient):
        resp = client.post(f"{B}/referrals", headers=doctor, json={
            "patient_id": patient, "program_code": "hypertension", "direction": "up", "reason": "血压控制不佳"})
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    def review(case_id):
        resp = client.post(f"{B}/referrals/{case_id}/review", headers=admin, json={"action": "pass"})
        assert resp.status_code == 200, resp.text

    stuck = refer(patients[0])           # 72 小时前发起，一直没人动
    _backdate(stuck, created_hours_ago=72, steps_hours_ago=[72])
    fresh = refer(patients[1])           # 72 小时前发起，刚刚审核通过
    _backdate(fresh, created_hours_ago=72, steps_hours_ago=[72])
    review(fresh)
    _backdate(fresh, created_hours_ago=72, steps_hours_ago=[72, 0])
    halfway = refer(patients[2])         # 100 小时前发起，60 小时前审核通过一格，之后卡住
    review(halfway)
    _backdate(halfway, created_hours_ago=100, steps_hours_ago=[100, 60])
    return {"doctor": doctor, "stuck": stuck, "fresh": fresh, "halfway": halfway}


def test_超时预警从最近一次推进起算_卡得最久的排最前(client, world):
    resp = client.get(f"{B}/referrals-alerts?hours=48", headers=world["doctor"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 修前 [halfway, stuck, fresh]：按建单时间判，刚审核过的也算超时，排序也按建单
    assert [c["id"] for c in body["items"]] == [world["stuck"], world["halfway"]]
    assert body["count"] == 2


def test_医生移动端工作台的超时转诊同一口径(client, world):
    resp = client.get(f"{B}/workbench/doctor-mobile", headers=world["doctor"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["referrals"]["overdue"] == 2   # 修前 3
