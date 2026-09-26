"""手术排班表不指定日期时给今天及以后的排班，不是最早那 300 条历史（P2-155）。

排班表的说明写「就是手术室墙上那张表」；不指定日期时实现按日期**升序**取前 300 条——排班一多（一天十来台，
不到一个月），桌面与移动端的「手术排班」（两处都不送日期）只剩最早那 300 条历史，明天的手术哪儿都看不见。
"""
from datetime import timedelta

import pytest

from app import clock


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.models import OperatingRoom, SurgeryRequest, SurgerySchedule, User

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2155 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2155 外科"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P2155-1"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2155 患者", "id_card": "330106196606061551", "gender": "男"}).json()["id"]
    admission = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "胆囊结石"}).json()["id"]
    today = clock.today()
    with SessionLocal() as db:
        operator = db.query(User).filter(User.username == "admin").one().id
        room = OperatingRoom(org_id=org, name="P2155 一号间")
        db.add(room)
        db.flush()

        def scheduled(day, name):
            request = SurgeryRequest(admission_id=admission, patient_id=patient, org_id=org, surgery_name=name,
                                     status="scheduled", created_by=operator)
            db.add(request)
            db.flush()
            db.add(SurgerySchedule(request_id=request.id, room_id=room.id, scheduled_date=day.isoformat(),
                                   start_time="08:00", end_time="09:00", created_by=operator))

        for i in range(300):   # 过去三百天、每天一台的历史
            scheduled(today - timedelta(days=300 - i), f"历史手术{i}")
        scheduled(today + timedelta(days=1), "明天的胆囊切除")
        db.commit()
    return {"tomorrow": (today + timedelta(days=1)).isoformat()}


def test_不指定日期_看得到明天的排班(client, admin, world):
    rows = client.get("/api/surgery/schedules", headers=admin).json()
    assert [(r["scheduled_date"], r["surgery_name"]) for r in rows] == [(world["tomorrow"], "明天的胆囊切除")]
    # 修前：最早那 300 条历史，没有明天这一台


def test_指定日期照旧按那一天查(client, admin, world):
    day = (clock.today() - timedelta(days=100)).isoformat()
    rows = client.get(f"/api/surgery/schedules?scheduled_date={day}", headers=admin).json()
    assert [r["surgery_name"] for r in rows] == ["历史手术200"]
