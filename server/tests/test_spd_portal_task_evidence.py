"""居民端「上传凭证」只存附件、不记进任务：要凭证的任务在手机上永远提交不了，医护也看不到凭证（P1-126）。

手机页（`m.js::renderSpdTasks`）的走法是：先点「上传凭证」传一份照片，页面提示「提交任务时会一并附上」；再点
「填报并提交」，交的是 `{result: {note}}`——**不带附件编号**。可上传接口只存附件、不写任务的佐证清单
（`spd_tasks.evidence`），提交时「要凭证的任务佐证清单为空」即 422「该任务需要上传照片或报告等凭证」。
医护端的佐证材料（`evidence_urls`）也取自这份清单，居民传上来的文件在审核页上看不到。

修法：居民为自己的任务上传的附件，就是这个任务的佐证——上传即记进佐证清单（去重）。提交时显式带附件编号的
老走法（`evidence` 整体替换）照旧。
"""
import pytest

B = "/api/spd"
P = "/api/portal/spd"
JPEG = b"\xff\xd8\xff\xe0p126-evidence"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.config import settings
    from app.database import SessionLocal
    from app.models import Patient, ResidentAccount

    with SessionLocal() as db:
        me = Patient(ehc_no="EHC-P126-ME", name="P126 居民", id_card="330106198001012126", gender="女",
                     birth_date="1980-01-01", phone="13912601126")
        db.add(me)
        db.flush()
        db.add(ResidentAccount(phone="13912601126", patient_id=me.id, nickname="P126", wechat_openid="",
                               status="active"))
        db.commit()
        patient_id = me.id
    old = settings.sms_debug_echo
    settings.sms_debug_echo = True
    try:
        code = client.post("/api/portal/auth/sms/code",
                           json={"phone": "13912601126", "purpose": "login"}).json()["debug_code"]
        token = client.post("/api/portal/auth/sms/login",
                            json={"phone": "13912601126", "code": code}).json()["access_token"]
    finally:
        settings.sms_debug_echo = old
    return {"patient": patient_id, "resident": {"Authorization": f"Bearer {token}"}}


def _task(client, admin, world, title):
    resp = client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": world["patient"], "title": title, "task_type": "report", "require_evidence": True})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _upload(client, world, task_id, content=JPEG):
    resp = client.post(f"{P}/tasks/{task_id}/attachments", headers=world["resident"],
                       files={"file": ("report.jpg", content, "image/jpeg")})
    assert resp.status_code == 201, resp.text
    return resp.json()["attachment_id"]


def test_照手机页的走法_先传凭证再只交填报内容_能提交_医护看得到(client, admin, world):
    task_id = _task(client, admin, world, "P126 上传检查报告")
    attachment_id = _upload(client, world, task_id)
    # 手机页「填报并提交」交的就是这个：只有填报内容，不带附件编号
    resp = client.post(f"{P}/tasks/{task_id}/submit", headers=world["resident"],
                       json={"result": {"note": "已上传报告"}})
    assert resp.status_code == 200 and resp.json() == {"id": task_id, "status": "submitted"}, resp.text  # 修前 422
    detail = client.get(f"{B}/tasks/{task_id}", headers=admin).json()
    assert detail["evidence"] == [attachment_id]                                    # 修前 []
    assert [e["attachment_id"] for e in detail["evidence_urls"]] == [attachment_id]  # 医护审核页的「佐证材料」


def test_传几份记几份_按上传先后_没传的照旧不让提交(client, admin, world):
    task_id = _task(client, admin, world, "P126 多份上传")
    resp = client.post(f"{P}/tasks/{task_id}/submit", headers=world["resident"], json={"result": {"note": "还没传"}})
    assert resp.status_code == 422 and resp.json() == {"detail": "该任务需要上传照片或报告等凭证"}
    # 同一份文件传两次也是两行附件（存储按内容去重，附件行不去重），佐证清单照记两条
    uploaded = [_upload(client, world, task_id, content)
                for content in (b"\xff\xd8\xff\xe0p126-twice", b"\xff\xd8\xff\xe0p126-twice",
                                b"\xff\xd8\xff\xe0p126-other")]
    assert len(set(uploaded)) == 3
    assert client.get(f"{B}/tasks/{task_id}", headers=admin).json()["evidence"] == uploaded


def test_提交时显式带附件编号的老走法照旧(client, admin, world):
    task_id = _task(client, admin, world, "P126 显式带编号")
    attachment_id = _upload(client, world, task_id, b"\xff\xd8\xff\xe0p126-explicit")
    resp = client.post(f"{P}/tasks/{task_id}/submit", headers=world["resident"],
                       json={"result": {}, "evidence": [attachment_id]})
    assert resp.status_code == 200, resp.text
    assert client.get(f"{B}/tasks/{task_id}", headers=admin).json()["evidence"] == [attachment_id]
