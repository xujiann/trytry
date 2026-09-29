"""居民端对已提交、待审核的任务不再收提交与凭证（P2-789，第二十一批「页面给出的动作 vs 后端允许的角色与状态」扫描
N4-6；医护端同一条是 P2-758）。

居民端 `submit_task` / `upload_task_evidence` 原先只挡已结束：另一台设备、代管家属的手机或没刷新的页面对待审核的任务
再提交一次照样 200，结果整段换掉、状态仍是待审核，审核人通过的是后写的那份；再传凭证照样追加进审核人看的佐证清单。
手机页本身对待审核的任务不给按钮，挡住的只是这一个页面。修后两处都 409（提交按条件翻转、只从能直接办结的状态翻；
上传在锁内重读再判）；审核退回之后照样能重新提交、再传凭证。
"""
import pytest

B = "/api/spd"
PHONE = "13912707891"
JPEG = ("report.jpg", b"\xff\xd8\xff\xe0fake-jpeg", "image/jpeg")


@pytest.fixture(scope="module")
def world(client, admin):
    from app.config import settings
    from app.database import SessionLocal
    from app.models import ResidentAccount

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2789 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2789 居民", "id_card": "330127196803032789", "phone": PHONE})
    assert patient.status_code == 201, patient.text
    patient_id = patient.json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient_id, "program_code": "hypertension", "org_id": org})
    assert enrollment.status_code == 201, enrollment.text
    with SessionLocal() as db:
        db.add(ResidentAccount(phone=PHONE, patient_id=patient_id, nickname="P2789", wechat_openid="",
                               status="active"))
        db.commit()
    old = settings.sms_debug_echo
    settings.sms_debug_echo = True
    try:
        code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE, "purpose": "login"}).json()["debug_code"]
        token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code}).json()["access_token"]
    finally:
        settings.sms_debug_echo = old
    return {"org": org, "patient": patient_id, "enrollment": enrollment.json()["id"],
            "resident": {"Authorization": f"Bearer {token}"}}


def _awaiting_review() -> str:
    from app.spd.routers.portal import RESIDENT_AWAITING_REVIEW   # 取在断言处：修前没有这个名字，先红在状态码上

    return RESIDENT_AWAITING_REVIEW


def _task(client, admin, world, title):
    resp = client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": world["patient"], "enrollment_id": world["enrollment"], "title": title,
        "task_type": "followup", "org_id": world["org"]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _submit(client, world, task, note):
    return client.post(f"/api/portal/spd/tasks/{task}/submit", headers=world["resident"],
                       json={"result": {"note": note}})


def _upload(client, world, task):
    return client.post(f"/api/portal/spd/tasks/{task}/attachments", headers=world["resident"], files={"file": JPEG})


def test_待审核的任务_居民再提交409_审核人看到的结果不被换掉(client, admin, world):
    task = _task(client, admin, world, "P2789 本周自测血压")
    first = _submit(client, world, task, "血压 128/82")
    assert first.status_code == 200 and first.json()["status"] == "submitted", first.text
    again = _submit(client, world, task, "血压 168/102（改）")
    assert again.status_code == 409, again.text   # 修前 200，结果整段换掉
    assert again.json()["detail"] == _awaiting_review()
    detail = client.get(f"{B}/tasks/{task}", headers=admin).json()
    assert (detail["status"], detail["result"]) == ("submitted", {"note": "血压 128/82"}), detail


def test_待审核的任务_居民再传凭证409_佐证清单不变(client, admin, world):
    task = _task(client, admin, world, "P2789 上传化验单")
    assert _submit(client, world, task, "已查").status_code == 200
    resp = _upload(client, world, task)
    assert resp.status_code == 409, resp.text   # 修前 201，追加进审核人看的佐证清单
    assert resp.json()["detail"] == _awaiting_review()
    assert client.get(f"{B}/tasks/{task}", headers=admin).json()["evidence"] == []


def test_审核退回之后_居民照样能补凭证重新提交(client, admin, world):
    task = _task(client, admin, world, "P2789 退回重做")
    assert _submit(client, world, task, "已测").status_code == 200
    rejected = client.post(f"{B}/tasks/{task}/review", headers=admin, json={"approved": False, "note": "请附血压计照片"})
    assert rejected.status_code == 200, rejected.text
    upload = _upload(client, world, task)
    assert upload.status_code == 201, upload.text
    resp = _submit(client, world, task, "已测，附照片")
    assert resp.status_code == 200 and resp.json()["status"] == "submitted", resp.text
    detail = client.get(f"{B}/tasks/{task}", headers=admin).json()
    assert detail["result"] == {"note": "已测，附照片"} and detail["evidence"] == [upload.json()["attachment_id"]]
