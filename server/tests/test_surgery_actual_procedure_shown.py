"""排班表与高值耗材追溯对做完的手术印实际术式 / 术者（P2-1400，第四十一批「手术与麻醉闭环」扫描 AE2-10）。

术中记录写实际术式与术者（中转开腹、换了主刀；P2-1307 起两端表单都录得进），「查看记录」与居民端「我的手术」（P2-556）
都按记录显示；手术排班表（`surgery.list_schedules`）与耗材追溯（`materials._consumable_out`，正向追溯、反向清单共用）原先
仍取申请单上的。扫描实测：申请「腹腔镜胆囊切除术 / 李住院」、记录「开腹（中转）/ 张主任」——排班表印「腹腔镜胆囊切除术
术者 李住院 completed」，耗材追溯印「腹腔镜胆囊切除术」，与「查看记录」、居民端对不上。

修后有术中记录的取记录上的实际术式与术者（术者空的回落申请单上的，取法同 P2-556），没有记录的照旧取申请单；出参的键与
次序不变。
"""
import pytest

from app.database import SessionLocal
from app.models import SurgeryRecord, SurgeryRequest, User

DAY = "2031-05-06"
ACTUAL = "开腹胆囊切除术（腹腔镜中转开腹）"
SCHEDULE_KEYS = ["id", "request_id", "room_name", "scheduled_date", "start_time", "end_time", "surgery_name",
                 "surgeon_name", "anesthesia_type", "urgency", "status"]


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21400 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21400 外科"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P21400-1"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21400 患者", "id_card": "330106197104051400"}).json()["id"]
    admission = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "胆囊结石"})
    assert admission.status_code == 201, admission.text
    room = client.post("/api/surgery/rooms", headers=admin, json={"org_id": org, "name": "P21400 一号间"}).json()["id"]
    staff = {}
    for username, full_name in (("p21400_res", "住院医李一"), ("p21400_sur", "外科张主任")):
        assert client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "full_name": full_name, "role": "doctor",
            "org_id": org}).status_code in (200, 201)
        token = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"}).json()
        staff[username] = {"Authorization": f"Bearer {token['access_token']}"}
    return {"org": org, "patient": patient, "admission": admission.json()["id"], "room": room,
            "resident": staff["p21400_res"], "surgeon": staff["p21400_sur"]}


def _scheduled(client, admin, world, name, hour, barcode):
    """住院医提申请（拟施术者空着，即申请人），管理员审批、排进 DAY 的一个时段，再把一枚耗材登记到这台上；返回申请号。"""
    req = client.post("/api/surgery/requests", headers=world["resident"], json={
        "admission_id": world["admission"], "surgery_name": name})
    assert req.status_code == 201, req.text
    rid = req.json()["id"]
    assert client.post(f"/api/surgery/requests/{rid}/approve", headers=admin, json={"approved": True}).status_code == 200
    sched = client.post(f"/api/surgery/requests/{rid}/schedule", headers=admin, json={
        "room_id": world["room"], "scheduled_date": DAY, "start_time": f"{hour:02d}:00", "end_time": f"{hour:02d}:50"})
    assert sched.status_code == 201, sched.text
    assert client.post("/api/materials/consumables", headers=admin, json={
        "barcode": barcode, "name": "高值耗材", "org_id": world["org"], "expire_date": "2039-12-31"}).status_code == 201
    used = client.post(f"/api/materials/consumables/{barcode}/use", headers=world["resident"],
                       json={"patient_id": world["patient"], "surgery_id": rid})
    assert used.status_code == 200, used.text
    return rid


def _board(client, admin, world):
    rows = client.get("/api/surgery/schedules", headers=admin,
                      params={"scheduled_date": DAY, "room_id": world["room"]}).json()
    return {r["request_id"]: r for r in rows}


def _traced(client, admin, world):
    """正向追溯（单条出参）与按患者的反向清单（按页一次取齐）各读一遍耗材记在哪台手术上。"""
    listed = client.get("/api/materials/consumables", headers=admin, params={"patient_id": world["patient"]}).json()
    by_list = {c["barcode"]: c["used_surgery_name"] for c in listed}
    by_trace = {b: client.get(f"/api/materials/consumables/trace/{b}", headers=admin).json()["used_surgery_name"]
                for b in by_list}
    return by_trace, by_list


def test_中转开腹又换了主刀_排班表与耗材追溯印实际的_没有记录的照旧(client, admin, world):
    done = _scheduled(client, admin, world, "腹腔镜胆囊切除术", 8, "P21400-CLIP")
    pending = _scheduled(client, admin, world, "腹股沟疝修补术", 13, "P21400-MESH")
    rec = client.post(f"/api/surgery/requests/{done}/record", headers=world["surgeon"], json={
        "actual_surgery_name": ACTUAL, "surgeon_name": "外科张主任", "outcome": "治愈",
        "start_at": f"{DAY} 08:10", "end_at": f"{DAY} 11:20"})
    assert rec.status_code == 201, rec.text

    board = _board(client, admin, world)
    # 修前：「腹腔镜胆囊切除术 / 住院医李一」——申请单上的，与「查看记录」、居民端对不上
    assert (board[done]["surgery_name"], board[done]["surgeon_name"], board[done]["status"]) == (
        ACTUAL, "外科张主任", "completed")
    # 还没做的照旧是申请单上的
    assert (board[pending]["surgery_name"], board[pending]["surgeon_name"], board[pending]["status"]) == (
        "腹股沟疝修补术", "住院医李一", "scheduled")
    assert list(board[done]) == SCHEDULE_KEYS and list(board[pending]) == SCHEDULE_KEYS   # 只改取值，键与次序不变

    by_trace, by_list = _traced(client, admin, world)
    expected = {"P21400-CLIP": ACTUAL, "P21400-MESH": "腹股沟疝修补术"}
    assert by_trace == expected   # 修前 P21400-CLIP 印「腹腔镜胆囊切除术」
    assert by_list == expected


def test_存量记录的术者是空串_回落申请单上的(client, admin, world):
    """取法同 P2-556：记录上的术者空着（录术者之前的存量），照旧署申请单上的，不印一个空。"""
    legacy = _scheduled(client, admin, world, "阑尾切除术", 15, "P21400-LEGACY")
    with SessionLocal() as db:
        creator = db.query(User.id).filter(User.username == "admin").scalar()
        db.add(SurgeryRecord(request_id=legacy, actual_surgery_name="腹腔镜阑尾切除术", surgeon_name="",
                             created_by=creator))
        db.get(SurgeryRequest, legacy).status = "completed"
        db.commit()
    row = _board(client, admin, world)[legacy]
    assert (row["surgery_name"], row["surgeon_name"]) == ("腹腔镜阑尾切除术", "住院医李一")   # 修前「阑尾切除术」
    by_trace, by_list = _traced(client, admin, world)
    assert by_trace["P21400-LEGACY"] == by_list["P21400-LEGACY"] == "腹腔镜阑尾切除术"
