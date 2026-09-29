"""术后随访的到期日从手术那天起算，不从术中记录的录入日起算（P2-896，第二十四批「时间窗口的边界」扫描 Z1-10）。

`create_record` 按 `今天 + SURGERY_FOLLOWUP_DAYS` 排术后随访，同一请求体里带着手术开始时刻 `start_at`：9-20 夜里做的手术
9-24 补录（开始时刻写 9-20），随访到期 10-08，应为 10-04。出院随访按实际出院日起算（P2-545），生命周期补登按发生日期
（P2-864）。修后取开始时刻的日期，没填取排班日，再没有才取今天。
"""
from datetime import timedelta

import pytest

from app import clock
from app.database import SessionLocal
from app.models import FollowupTask, OperatingRoom, SurgeryRequest, SurgerySchedule, User
from app.routers.surgery import SURGERY_FOLLOWUP_DAYS


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2896 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2896 外科"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P2896-1"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2896 患者", "id_card": "330106196606062896", "gender": "男"}).json()["id"]
    admission = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "急性阑尾炎"}).json()["id"]
    operated = clock.today() - timedelta(days=4)
    with SessionLocal() as db:
        operator = db.query(User).filter(User.username == "admin").one().id
        room = OperatingRoom(org_id=org, name="P2896 一号间")
        db.add(room)
        db.flush()
        requests = []
        for name in ("阑尾切除术", "疝修补术"):
            request = SurgeryRequest(admission_id=admission, patient_id=patient, org_id=org, surgery_name=name,
                                     status="scheduled", created_by=operator)
            db.add(request)
            db.flush()
            db.add(SurgerySchedule(request_id=request.id, room_id=room.id, scheduled_date=operated.isoformat(),
                                   start_time=f"{20 + len(requests)}:00", end_time=f"{21 + len(requests)}:00",
                                   created_by=operator))
            requests.append(request.id)
        db.commit()
    return {"requests": requests, "operated": operated}


def _due(request_id):
    with SessionLocal() as db:
        return db.query(FollowupTask.due_date).filter(FollowupTask.category == "surgery",
                                                      FollowupTask.source_id == request_id).scalar()


def test_补录的术中记录_随访从手术开始那天起算(client, admin, world):
    request = world["requests"][0]
    got = client.post(f"/api/surgery/requests/{request}/record", headers=admin, json={
        "actual_surgery_name": "阑尾切除术", "start_at": f"{world['operated'].isoformat()} 22:30"})
    assert got.status_code == 201, got.text
    assert _due(request) == (world["operated"] + timedelta(days=SURGERY_FOLLOWUP_DAYS)).isoformat()   # 修前晚 4 天


def test_没填开始时刻_按排班日起算(client, admin, world):
    request = world["requests"][1]
    got = client.post(f"/api/surgery/requests/{request}/record", headers=admin, json={"actual_surgery_name": "疝修补术"})
    assert got.status_code == 201, got.text
    assert _due(request) == (world["operated"] + timedelta(days=SURGERY_FOLLOWUP_DAYS)).isoformat()
