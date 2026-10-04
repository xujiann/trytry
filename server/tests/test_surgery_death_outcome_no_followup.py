"""术中记录转归「死亡」，照样给家属发「术后随访已安排」并排术后随访（P2-499，第九批「通知承诺」扫描 W2-4）。

`POST /api/surgery/requests/{id}/record` 填完术中记录即结案，并「自动生成术后随访任务」、给患者发「您的XX已完成，我们将
在 N 天内与您联系随访」——不看同一个请求里的转归。转归「死亡」的，消息发到死者名下（家属在居民端看得到），术后随访任务
到期被扫成超期、挂进随访督办。出院随访不看转归是 P2-385（待裁定：出院时病案首页的转归可能还没写）；这里转归与记录同一个
请求，没有先后问题。

修法：转归「死亡」不排术后随访、不发这条消息；其余转归照旧。
"""
import pytest

PHONE = "13900024990"
ID_CARD = "330281197005052499"
# 「其余转归照旧」那条用另一位患者的住院：转归「死亡」之后同一次住院不再收新申请（P2-1396）
PHONE2 = "13900024991"
ID_CARD2 = "330281197006062499"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2499 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"name": "P2499 外科", "org_id": org}).json()
    admissions = {}
    for outcome, name, id_card, phone in (("死亡", "P2499 患者", ID_CARD, PHONE),
                                          ("好转", "P2499 患者乙", ID_CARD2, PHONE2)):
        patient = client.post("/api/patients", headers=admin, json={
            "name": name, "id_card": id_card, "phone": phone}).json()["id"]
        # 家属 / 本人在居民端绑了这份档案，才看得出消息发没发
        code = client.post("/api/portal/auth/sms/code", json={"phone": phone}).json()["debug_code"]
        token = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code}).json()["access_token"]
        bound = client.post("/api/portal/auth/realname", json={"name": name, "id_card": id_card},
                            headers={"Authorization": f"Bearer {token}"})
        assert bound.status_code in (200, 409), bound.text
        bed = client.post("/api/inpatient/beds", headers=admin, json={
            "ward_id": ward["id"], "bed_no": f"P2499-{len(admissions) + 1}"}).json()
        admission = client.post("/api/inpatient/admissions", headers=admin, json={
            "patient_id": patient, "ward_id": ward["id"], "bed_id": bed["id"], "doctor_name": "P2499 医生",
            "diagnosis_name": "急性阑尾炎"})
        assert admission.status_code == 201, admission.text
        admissions[outcome] = admission.json()["id"]
    room = client.post("/api/surgery/rooms", headers=admin, json={"org_id": org, "name": "P2499 手术间"}).json()
    # 申请人不得自批（职责分离）：申请与术中记录由本院医生来，审批由管理员来
    assert client.post("/api/users", headers=admin, json={
        "username": "p2499_doc", "password": "passw0rd1", "full_name": "P2499 医生", "role": "doctor",
        "org_id": org}).status_code in (200, 201)
    doctor = client.post("/api/auth/login", json={"username": "p2499_doc", "password": "passw0rd1"}).json()
    return {"admissions": admissions, "room": room["id"],
            "doctor": {"Authorization": f"Bearer {doctor['access_token']}"}}


def _record(client, admin, world, name, start, outcome):
    req = client.post("/api/surgery/requests", headers=world["doctor"], json={
        "admission_id": world["admissions"][outcome], "surgery_name": name})
    assert req.status_code == 201, req.text
    rid = req.json()["id"]
    assert client.post(f"/api/surgery/requests/{rid}/approve", headers=admin,
                       json={"approved": True}).status_code == 200
    sched = client.post(f"/api/surgery/requests/{rid}/schedule", headers=admin, json={
        "room_id": world["room"], "scheduled_date": "2031-03-01", "start_time": start,
        "end_time": f"{int(start[:2]) + 1:02d}:00"})
    assert sched.status_code == 201, sched.text
    rec = client.post(f"/api/surgery/requests/{rid}/record", headers=world["doctor"], json={
        "actual_surgery_name": name, "outcome": outcome})
    assert rec.status_code == 201 and rec.json()["outcome"] == outcome, rec.text
    return rid


def _aftercare(rid):
    from app.database import SessionLocal
    from app.models import FollowupTask, Notification

    with SessionLocal() as db:
        tasks = db.query(FollowupTask).filter(FollowupTask.category == "surgery", FollowupTask.source_id == rid).count()
        notices = [n.title for n in db.query(Notification).filter(   # 排班时的「手术已安排」不算
            Notification.link_type == "surgery_request", Notification.link_id == rid,
            Notification.category == "followup")]
        return tasks, notices


def test_转归死亡不排术后随访_不发随访已安排(client, admin, world):
    rid = _record(client, admin, world, "P2499 剖腹探查术", "08:00", "死亡")
    assert _aftercare(rid) == (0, [])   # 修前 (1, ["术后随访已安排"])


def test_其余转归照旧排随访_发消息(client, admin, world):
    rid = _record(client, admin, world, "P2499 阑尾切除术", "10:00", "好转")
    assert _aftercare(rid) == (1, ["术后随访已安排"])
