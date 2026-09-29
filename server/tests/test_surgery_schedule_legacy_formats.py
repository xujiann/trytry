"""手术排班按日历读存量日期与时刻：旧排班不再挂在「今天及以后」，同一手术间也排不进重叠的一台（P2-894，第二十四批「存量行 vs 新规则」扫描 Z3-4）。

P1-61 / P2-46（09-24）之前排班的日期、时刻都不卡形状，存量里有「2026/10/05」「2026-9-5」「８:００」「１０:００」这类写法：
- 排班表不指定日期时按 `scheduled_date >= 今天` 字符串比（P2-155）：'/'、'9' 都比 '-'、'0' 大，早过去的旧排班一直挂着；
- 冲突判定按 `scheduled_date ==` 等值再比时刻字符串：「2026/10/05 8:00-１０:００」占着的手术间，10-05 08:30 再排一台 201。
修后两处都按日历读（`datetypes.legacy_date` / `legacy_time`）；时刻认不出的按占满当天算。
"""
from datetime import timedelta

import pytest

from app import clock
from app.database import SessionLocal
from app.models import OperatingRoom, SurgeryRequest, SurgerySchedule, User


def _slash(day):
    return f"{day.year}/{day.month}/{day.day}"   # 修前存下的斜杠写法，不补零


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2894 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2894 外科"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P2894-1"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2894 患者", "id_card": "330106196606062894", "gender": "男"}).json()["id"]
    admission = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "胆囊结石"}).json()["id"]
    today = clock.today()
    future, past = today + timedelta(days=6), today - timedelta(days=24)
    with SessionLocal() as db:
        operator = db.query(User).filter(User.username == "admin").one().id
        room = OperatingRoom(org_id=org, name="P2894 一号间")
        db.add(room)
        db.flush()

        def request(name, status):
            row = SurgeryRequest(admission_id=admission, patient_id=patient, org_id=org, surgery_name=name,
                                 status=status, created_by=operator)
            db.add(row)
            db.flush()
            return row.id

        for day, name in ((future, "存量·将来"), (past, "存量·已过去")):
            db.add(SurgerySchedule(request_id=request(name, "scheduled"), room_id=room.id, scheduled_date=_slash(day),
                                   start_time="8:00", end_time="１０:００", created_by=operator))
        pending = [request(f"新排{i}", "approved") for i in range(2)]
        db.commit()
        room_id = room.id
    return {"room": room_id, "future": future.isoformat(), "pending": pending}


def test_存量占着的时段_再排一台409_错开的照排(client, admin, world):
    def schedule(request_id, start, end):
        return client.post(f"/api/surgery/requests/{request_id}/schedule", headers=admin, json={
            "room_id": world["room"], "scheduled_date": world["future"], "start_time": start, "end_time": end})

    got = schedule(world["pending"][0], "08:30", "09:30")
    assert got.status_code == 409, got.text   # 修前 201：同一手术间同一时段排进两台
    assert schedule(world["pending"][0], "10:00", "11:00").status_code == 201


def test_排班表按日历筛与排(client, admin, world):
    rows = client.get("/api/surgery/schedules", headers=admin, params={"room_id": world["room"]}).json()
    assert [r["surgery_name"] for r in rows] == ["存量·将来", "新排0"]   # 修前还挂着「存量·已过去」
    same_day = client.get("/api/surgery/schedules", headers=admin,
                          params={"room_id": world["room"], "scheduled_date": world["future"]}).json()
    assert [r["surgery_name"] for r in same_day] == ["存量·将来", "新排0"]   # 修前查不到斜杠写法的那台
