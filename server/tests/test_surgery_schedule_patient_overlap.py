"""同一患者的两台手术排进两个手术间的重叠时段，两单都 201（P2-1397，第四十一批「手术与麻醉闭环」扫描 AE2-2 的同一患者那一半）。

排班原先只按手术间判重叠：甲患者的两台排在 1号间 08:00-10:00 和 2号间 09:00-11:00，两单都 201，居民收到两条时段重叠的
「手术已安排」。修法：同一患者同一天时段重叠的另一台未取消排班存在即 409；判定与写入在患者那一行的临界区里（同一患者跨两个
手术间并发排班，两路锁的是不同手术间、互不阻塞），锁序先患者、后手术间。日期、时刻的存量非规范写法照手术间判定的
`legacy_date` / `legacy_time` 口径读。术者、麻醉医师撞台是业务口径，不在此列。
"""
import contextlib

import pytest

from conftest import login

from app.database import SessionLocal
from app.models import Notification, SurgeryRequest, SurgerySchedule, User
from app.routers import surgery

S = "/api/surgery"
DAY, OTHER_DAY, LEGACY_DAY, CANCELLED_DAY, LOCK_DAY = "2031-04-01", "2031-04-02", "2031-04-03", "2031-04-04", "2031-04-05"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21397 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    # 申请人不得自批（职责分离）：申请由本院医生提，审批、排班由管理员来
    resp = client.post("/api/users", headers=admin, json={
        "username": "p21397_doc", "password": "passw0rd1", "full_name": "P21397 医生", "role": "doctor", "org_id": org})
    assert resp.status_code in (200, 201), resp.text
    doctor = login(client, "p21397_doc", "passw0rd1")
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21397 外科"}).json()["id"]
    rooms = [client.post(f"{S}/rooms", headers=admin, json={"org_id": org, "name": f"P21397 {n}号间"}).json()["id"]
             for n in (1, 2, 3, 4)]
    patients, admissions = {}, {}
    for n, (key, id_card, phone) in enumerate((("甲", "330102197001011397", "13900213971"),
                                               ("乙", "330102197002021397", "13900213972"))):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P21397 {key}患者", "id_card": id_card, "phone": phone})
        assert patient.status_code in (200, 201), patient.text
        # 本人在居民端绑了这份档案，才看得出「手术已安排」发了几条
        code = client.post("/api/portal/auth/sms/code", json={"phone": phone}).json()["debug_code"]
        token = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code}).json()["access_token"]
        bound = client.post("/api/portal/auth/realname", json={"name": f"P21397 {key}患者", "id_card": id_card},
                            headers={"Authorization": f"Bearer {token}"})
        assert bound.status_code in (200, 409), bound.text
        bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": f"P21397-{n}"}).json()
        adm = client.post("/api/inpatient/admissions", headers=admin, json={
            "patient_id": patient.json()["id"], "ward_id": ward, "bed_id": bed["id"], "diagnosis_name": "胆囊结石"})
        assert adm.status_code == 201, adm.text
        patients[key], admissions[key] = patient.json()["id"], adm.json()["id"]
    return {"org": org, "doctor": doctor, "rooms": rooms, "patients": patients, "admissions": admissions}


def _approved(client, admin, world, who, name):
    resp = client.post(f"{S}/requests", headers=world["doctor"], json={
        "admission_id": world["admissions"][who], "surgery_name": name})
    assert resp.status_code == 201, resp.text
    rid = resp.json()["id"]
    assert client.post(f"{S}/requests/{rid}/approve", headers=admin, json={"approved": True}).status_code == 200
    return rid


def _schedule(client, admin, rid, room, day, start, end):
    return client.post(f"{S}/requests/{rid}/schedule", headers=admin, json={
        "room_id": room, "scheduled_date": day, "start_time": start, "end_time": end})


def _arranged(rid):
    """这张申请的排班行数、给居民发的「手术已安排」条数、申请状态。"""
    with SessionLocal() as db:
        schedules = db.query(SurgerySchedule).filter(SurgerySchedule.request_id == rid).count()
        notices = db.query(Notification).filter(
            Notification.link_type == "surgery_request", Notification.link_id == rid,
            Notification.title.like("手术已安排%")).count()
        return schedules, notices, db.get(SurgeryRequest, rid).status


@pytest.fixture(scope="module")
def first(client, admin, world):
    """甲患者的第一台：1号间 08:00-10:00。"""
    rid = _approved(client, admin, world, "甲", "P21397 腹腔镜胆囊切除术")
    resp = _schedule(client, admin, rid, world["rooms"][0], DAY, "08:00", "10:00")
    assert resp.status_code == 201, resp.text
    assert _arranged(rid) == (1, 1, "scheduled")
    return rid


def test_同一患者同一天时段重叠的另一台_409_不发第二条手术已安排(client, admin, world, first):
    rid = _approved(client, admin, world, "甲", "P21397 胃镜下息肉切除术")
    resp = _schedule(client, admin, rid, world["rooms"][1], DAY, "09:00", "11:00")
    assert resp.status_code == 409, resp.text   # 修前 201：另一个手术间、时段重叠，居民收到两条「手术已安排」
    assert resp.json()["detail"] == "该患者在 08:00-10:00 已排有另一台手术，同一患者的手术时段不能重叠"
    assert _arranged(rid) == (0, 0, "approved")
    # 换一天就照排
    assert _schedule(client, admin, rid, world["rooms"][1], OTHER_DAY, "09:00", "11:00").status_code == 201


def test_同一患者时段不重叠的照排(client, admin, world, first):
    rid = _approved(client, admin, world, "甲", "P21397 胆总管探查术")
    resp = _schedule(client, admin, rid, world["rooms"][2], DAY, "10:00", "12:00")   # 接在第一台 10:00 收台之后
    assert resp.status_code == 201, resp.text
    assert _arranged(rid) == (1, 1, "scheduled")


def test_不同患者同一时段照常(client, admin, world, first):
    rid = _approved(client, admin, world, "乙", "P21397 疝修补术")
    resp = _schedule(client, admin, rid, world["rooms"][3], DAY, "09:00", "11:00")
    assert resp.status_code == 201, resp.text


def test_存量非规范写法照日历读_已取消的不算(client, admin, world):
    """存量排班的日期 / 时刻是 P1-61 / P2-46 之前的写法（斜杠、不补零、全角）照日历读；已取消那张的排班不算（取消路径随
    P2-182，这里直接落库造一条）。"""
    with SessionLocal() as db:
        operator = db.query(User).filter(User.username == "admin").one().id
        for status, day in (("scheduled", "2031/4/3"), ("cancelled", CANCELLED_DAY)):
            old = SurgeryRequest(admission_id=world["admissions"]["甲"], patient_id=world["patients"]["甲"],
                                 org_id=world["org"], surgery_name=f"P21397 存量{status}", status=status,
                                 created_by=operator)
            db.add(old)
            db.flush()
            db.add(SurgerySchedule(request_id=old.id, room_id=world["rooms"][0], scheduled_date=day,
                                   start_time="８:００", end_time="１０:００", created_by=operator))
        db.commit()
    legacy = _approved(client, admin, world, "甲", "P21397 存量那天再排一台")
    resp = _schedule(client, admin, legacy, world["rooms"][1], LEGACY_DAY, "09:00", "10:00")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "该患者在 ８:００-１０:００ 已排有另一台手术，同一患者的手术时段不能重叠"
    after_cancelled = _approved(client, admin, world, "甲", "P21397 已取消那天再排一台")
    resp = _schedule(client, admin, after_cancelled, world["rooms"][1], CANCELLED_DAY, "09:00", "10:00")
    assert resp.status_code == 201, resp.text


def test_先锁患者_后锁手术间(client, admin, world, monkeypatch):
    """同一患者跨两个手术间并发排班，要在患者那一行上排队（SQLite 上判不出并发，这里钉住两把锁都拿、次序固定）。"""
    taken = []
    real = surgery.serialized_on

    @contextlib.contextmanager
    def recording(db, model, row_id):
        taken.append((model.__name__, row_id))
        with real(db, model, row_id):
            yield

    monkeypatch.setattr(surgery, "serialized_on", recording)
    rid = _approved(client, admin, world, "乙", "P21397 锁序")
    resp = _schedule(client, admin, rid, world["rooms"][0], LOCK_DAY, "08:00", "09:00")
    assert resp.status_code == 201, resp.text
    assert taken == [("Patient", world["patients"]["乙"]), ("OperatingRoom", world["rooms"][0])]   # 修前只锁手术间
