"""居民上传佐证时清单是锁外 JSON 读改写：一次选两张照片、两路上传同时到，后写的把先写的编号盖掉（P2-303）。

`upload_task_evidence` 上传即把附件编号记进任务的佐证清单（P1-126）——可清单是 JSON 列表，只能「读旧值 + 本次 →
整体写回」，任务又是在函数开头锁外读的：两路上传交错，后提交的一路手上是没有对方编号的旧清单，写回去对方那张就不在
清单里了（附件照存，医护审核页看不到，要凭证的提交也可能因此不够）。读改写闸门认的是 `obj.col = … obj.col …`，这里
隔了一个局部变量，它没看见。

修法：锁住任务这一行、重读、再追加（`serialized_on`，先进临界区再写库）。这里把「这一路读到任务之后、写回之前，另一路
先把自己的编号记进去了」钉成确定的时序：在两者之间必经的附件落库里插进另一路的提交。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"
P = "/api/portal/spd"
PHONE = "13912602303"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.config import settings
    from app.models import Patient, ResidentAccount

    with SessionLocal() as db:
        me = Patient(ehc_no="EHC-P2303", name="P2303 居民", id_card="330106198001012303", gender="女",
                     birth_date="1980-01-01", phone=PHONE)
        db.add(me)
        db.flush()
        db.add(ResidentAccount(phone=PHONE, patient_id=me.id, nickname="P2303", wechat_openid="", status="active"))
        db.commit()
        patient_id = me.id
    old = settings.sms_debug_echo
    settings.sms_debug_echo = True
    try:
        code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE, "purpose": "login"}).json()["debug_code"]
        token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code}).json()["access_token"]
    finally:
        settings.sms_debug_echo = old
    task = client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": patient_id, "title": "P2303 上传检查报告", "task_type": "report", "require_evidence": True})
    assert task.status_code == 201, task.text
    return {"task": task.json()["id"], "resident": {"Authorization": f"Bearer {token}"}}


def test_两路上传交错_两个编号都在清单里(client, admin, world, monkeypatch):
    from app.spd.models import SpdTask
    from app.spd.routers import portal

    real, other_id = portal.store_attachment, 987654   # 另一路上传记进去的编号（清单只存编号，不查附件表）

    def racing(db, **kwargs):
        attachment = real(db, **kwargs)
        with SessionLocal() as other:   # 这一路读到任务之后、写回清单之前，另一路先记好了自己的那张
            task = other.get(SpdTask, world["task"])
            task.evidence = [*(task.evidence or []), other_id]
            other.commit()
        return attachment

    monkeypatch.setattr(portal, "store_attachment", racing)
    got = client.post(f"{P}/tasks/{world['task']}/attachments", headers=world["resident"],
                      files={"file": ("p2303.jpg", b"\xff\xd8\xff\xe0p2303", "image/jpeg")})
    monkeypatch.undo()
    assert got.status_code == 201, got.text
    evidence = client.get(f"{B}/tasks/{world['task']}", headers=admin).json()["evidence"]
    assert evidence == [other_id, got.json()["attachment_id"]]   # 修前只剩这一路的编号
