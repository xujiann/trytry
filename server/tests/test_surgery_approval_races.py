"""手术申请一个批准、一个驳回同时到：驳回被批准盖掉，经办照「已审批」排进手术间（P2-1184，第三十四批扫描 L1-3）。

`approve_request` 原先锁外判「待审批」、往对象上赋值再提交，那条 UPDATE 只有 `WHERE id = ?`：主任甲的批准判完还没写，
主任乙的驳回先提交了，甲这一路照旧把申请整行写成「已审批」，两路都 200——驳回的主任以为已经否决，申请却是「已审批」，
经办照常排进手术间、患者收到「手术已安排」。同形的物资采购（P2-403）、药品采购（P2-759）、用血（P2-110）审批早已压进
条件 UPDATE。

修法同它们：审批与「还待审批」同一条 UPDATE（`concurrency.move_row`），抢输的一路回滚、按库里的现状 409，文案与顺序
请求同一句。这里把「判过了、还没写」钉成确定的时序：批准一路取审批时刻（`utcnow`，判定之后、写入之前）的那一刻，
经真实接口插一路驳回并提交。
"""
import pytest

from conftest import login

from app.database import SessionLocal
from app.models import SurgeryRequest

B = "/api/surgery"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21184 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    heads = {}
    for username, role in (("p21184_dir_a", "director"), ("p21184_dir_b", "director"), ("p21184_op", "operator")):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": role, "org_id": org, "full_name": username})
        assert created.status_code == 201, created.text
        heads[username] = login(client, username, "passw0rd1")
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21184 外科病区"}).json()["id"]
    room = client.post(f"{B}/rooms", headers=admin, json={"org_id": org, "name": "P21184 一号手术间"})
    assert room.status_code == 201, room.text
    return {"org": org, "ward": ward, "room": room.json()["id"], "n": 0, **heads}


def _request(client, admin, world):
    """一位在院患者的一张待审批手术申请（申请人是 admin，与两位审批主任不是同一人）。"""
    world["n"] += 1
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": world["ward"], "bed_no": f"P21184-{world['n']}"})
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P21184 患者{world['n']}", "id_card": f"33012719800{world['n']}011184"}).json()["id"]
    admission = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": world["ward"], "bed_id": bed.json()["id"]})
    assert admission.status_code == 201, admission.text
    got = client.post(f"{B}/requests", headers=admin, json={
        "admission_id": admission.json()["id"], "surgery_name": "腹腔镜胆囊切除术"})
    assert got.status_code == 201 and got.json()["status"] == "requested", got.text
    return got.json()["id"]


def _status(request_id):
    with SessionLocal() as db:
        return db.get(SurgeryRequest, request_id).status


def _schedule(client, world, request_id, start="09:00", end="11:00"):
    return client.post(f"{B}/requests/{request_id}/schedule", headers=world["p21184_op"], json={
        "room_id": world["room"], "scheduled_date": "2026-10-08", "start_time": start, "end_time": end})


def test_批准判完还没写时驳回先提交_批准409_驳回不被盖掉_也排不了班(client, admin, world, monkeypatch):
    from app.routers import surgery

    request_id = _request(client, admin, world)
    real, fired = surgery.utcnow, []

    def racing():
        if not fired:   # 先记上：插进来的驳回自己也取审批时刻
            fired.append("驳回")
            fired.append(client.post(f"{B}/requests/{request_id}/approve", headers=world["p21184_dir_b"],
                                     json={"approved": False, "note": "指征不足，驳回"}))
        return real()

    monkeypatch.setattr(surgery, "utcnow", racing)
    got = client.post(f"{B}/requests/{request_id}/approve", headers=world["p21184_dir_a"], json={"approved": True})
    monkeypatch.undo()
    assert fired, "插桩没有触发：审批不再取 utcnow 了，换一个判定之后、写入之前的插点"
    rejected = fired[1]
    assert rejected.status_code == 200 and rejected.json()["status"] == "cancelled", rejected.text
    assert got.status_code == 409, got.text   # 修前 200 {'status': 'approved'}
    assert got.json() == {"detail": "当前状态 已取消 不可审批"}   # 按库里现状措辞，与顺序请求同一句
    assert _status(request_id) == "cancelled"   # 修前 approved：驳回被批准盖掉
    scheduled = _schedule(client, world, request_id)
    assert scheduled.status_code == 409, scheduled.text   # 修前 201：照「已审批」排进手术间
    assert scheduled.json() == {"detail": "当前状态 已取消 不可排班"}


def test_不并发时照常审批_再审批按现状409(client, admin, world):
    approved = _request(client, admin, world)
    got = client.post(f"{B}/requests/{approved}/approve", headers=world["p21184_dir_a"], json={"approved": True})
    assert got.status_code == 200 and got.json() == {"id": approved, "status": "approved"}, got.text
    again = client.post(f"{B}/requests/{approved}/approve", headers=world["p21184_dir_b"], json={"approved": False})
    assert again.status_code == 409 and again.json() == {"detail": "当前状态 已审批 不可审批"}, again.text
    assert _status(approved) == "approved"
    assert _schedule(client, world, approved, "13:00", "15:00").status_code == 201

    cancelled = _request(client, admin, world)
    got = client.post(f"{B}/requests/{cancelled}/approve", headers=world["p21184_dir_b"], json={"approved": False})
    assert got.status_code == 200 and got.json() == {"id": cancelled, "status": "cancelled"}, got.text
    again = client.post(f"{B}/requests/{cancelled}/approve", headers=world["p21184_dir_a"], json={"approved": True})
    assert again.status_code == 409 and again.json() == {"detail": "当前状态 已取消 不可审批"}, again.text
    with SessionLocal() as db:
        row = db.get(SurgeryRequest, cancelled)
        assert (row.status, row.approved_at is not None) == ("cancelled", True)
