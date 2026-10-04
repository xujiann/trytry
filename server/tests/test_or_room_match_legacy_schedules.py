"""手术间撮合与排班判冲突共用同一个占用判定：P1-61 之前存下的非规范日期 / 时刻，撮合不再说「可用」（P2-1401，第四十一批
「手术与麻醉闭环」扫描 AE2-7）。

撮合（`resources.match_operating_rooms`）的说明写「冲突判定与排班接口用的是同一套……刻意不另写一份——撮合说能排、排班说
冲突，比没有撮合更糟」；可排班自 P2-894 起把同手术间当天的、加上日期不是规范写法的存量行按日历读（`legacy_date` /
`legacy_time`，时刻认不出的按占满当天），撮合仍按 `scheduled_date == 当天` 等值查、时刻按字符串比。扫描实测：存量排班
「2026-10-6 08:00-10:00」在 1号间，撮合 10-06 返回 1号间可用、没有冲突；照撮合排 08:30-09:30 → 409「手术间在 08:00-10:00
已被占用」。

修后两处都调 `surgery.room_occupancy`：撮合的冲突与空档按它算，冲突时段照排班 409 的写法印库里存的起止。规范写法的照旧
（`test_stage10_resources.py`、`test_resources_contract.py` 钉着）。
"""
import pytest

from app.database import SessionLocal
from app.models import OperatingRoom, SurgeryRequest, SurgerySchedule, User

DAY = "2031-06-05"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21401 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21401 外科"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P21401-1"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21401 患者", "id_card": "330106197206051401"}).json()["id"]
    admission = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "胆囊结石"}).json()["id"]
    # 存量那几台挂在另一位患者名下：同一患者同日时段重叠的排班也 409（P2-1397），「上午」那台时刻认不出、按占满当天算，
    # 与新排的同一位患者就会撞上——本文件钉的是手术间的占用判定，不是同一患者的
    legacy_bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P21401-2"}).json()["id"]
    legacy_patient = client.post("/api/patients", headers=admin, json={
        "name": "P21401 存量患者", "id_card": "330106197406051409"}).json()["id"]
    legacy_admission = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": legacy_patient, "ward_id": ward, "bed_id": legacy_bed, "diagnosis_name": "胆囊结石"}).json()["id"]
    with SessionLocal() as db:
        creator = db.query(User.id).filter(User.username == "admin").scalar()
        rooms = [OperatingRoom(org_id=org, name=f"P21401 {n}号间") for n in (1, 2, 3)]
        db.add_all(rooms)
        db.flush()

        def request(name, status, adm=admission, pat=patient):
            row = SurgeryRequest(admission_id=adm, patient_id=pat, org_id=org, surgery_name=name,
                                 status=status, created_by=creator)
            db.add(row)
            db.flush()
            return row.id

        # P1-61 / P2-46 之前存下的三种写法：日期不补零 + 时刻不补零、斜杠日期 + 全角时刻、时刻认不出
        for room, day, start, end in ((rooms[0], "2031-6-5", "8:00", "9:00"),
                                      (rooms[1], "2031/06/05", "１３:００", "１５:００"),
                                      (rooms[2], "2031-6-5", "上午", "")):
            db.add(SurgerySchedule(request_id=request("存量手术", "scheduled", legacy_admission, legacy_patient),
                                   room_id=room.id, scheduled_date=day,
                                   start_time=start, end_time=end, created_by=creator))
        pending = [request(f"新排{i}", "approved") for i in range(5)]
        db.commit()
        room_ids = [r.id for r in rooms]
    # 1号间再按接口排一台规范写法的 09:30-10:30：按字符串比「8:00」排在「09:30」之后，空档得按时刻读了再切
    assert client.post(f"/api/surgery/requests/{pending[0]}/schedule", headers=admin, json={
        "room_id": room_ids[0], "scheduled_date": DAY, "start_time": "09:30", "end_time": "10:30"}).status_code == 201
    return {"org": org, "rooms": room_ids, "pending": pending[1:]}


def _match(client, admin, world, start, end):
    body = client.get("/api/resources/match/or-rooms", headers=admin, params={
        "org_id": world["org"], "scheduled_date": DAY, "start_time": start, "end_time": end}).json()
    return {r["room_id"]: r for r in body["rooms"]}


def _schedule(client, admin, world, room, start, end):
    request_id = world["pending"].pop()
    got = client.post(f"/api/surgery/requests/{request_id}/schedule", headers=admin, json={
        "room_id": room, "scheduled_date": DAY, "start_time": start, "end_time": end})
    if got.status_code != 201:
        world["pending"].append(request_id)   # 没排进去，这张申请留着下一次用
    return got


def test_存量写法占着的手术间_撮合判冲突与空档(client, admin, world):
    first, second, third = (_match(client, admin, world, "08:00", "18:00")[room] for room in world["rooms"])
    # 修前 2、3号间「可用、没有冲突、空档 08:00-18:00」；1号间只看得见规范写法的 09:30-10:30，空档里含着存量那台的 8:00-9:00
    assert (first["available"], first["conflicts"], first["gaps"]) == (False, [
        {"start_time": "8:00", "end_time": "9:00"}, {"start_time": "09:30", "end_time": "10:30"}], [
        {"start_time": "09:00", "end_time": "09:30"}, {"start_time": "10:30", "end_time": "18:00"}])
    assert (second["available"], second["conflicts"], second["gaps"]) == (False, [
        {"start_time": "１３:００", "end_time": "１５:００"}], [
        {"start_time": "08:00", "end_time": "13:00"}, {"start_time": "15:00", "end_time": "18:00"}])
    # 时刻认不出的按占满当天：与排班同一句「宁可拦下让人核对」，整个窗口没有空档
    assert (third["available"], third["conflicts"], third["gaps"]) == (False, [
        {"start_time": "上午", "end_time": ""}], [])


def test_撮合说有冲突的排班也409_撮合给的空档照排(client, admin, world):
    first, second, third = world["rooms"]
    for room, start, end, taken in ((first, "08:30", "09:30", "8:00-9:00"),
                                    (second, "14:00", "14:30", "１３:００-１５:００"),
                                    (third, "20:00", "21:00", "上午-")):
        assert _match(client, admin, world, start, end)[room]["available"] is False   # 修前 True
        got = _schedule(client, admin, world, room, start, end)
        assert (got.status_code, got.json()["detail"]) == (409, f"手术间在 {taken} 已被占用"), got.text
    # 撮合给的空档照排得进
    assert _match(client, admin, world, "09:00", "09:30")[first]["available"] is True
    assert _schedule(client, admin, world, first, "09:00", "09:30").status_code == 201
    assert _match(client, admin, world, "15:00", "16:00")[second]["available"] is True
    assert _schedule(client, admin, world, second, "15:00", "16:00").status_code == 201
