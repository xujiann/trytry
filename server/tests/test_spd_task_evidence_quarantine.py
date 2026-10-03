"""被病毒扫描隔离的附件仍算「佐证材料」：要佐证的任务凭一张一下载就 410 的附件照样提交、办结（P2-1251，第三十六批扫描 U2-3）。

`platform.valid_task_evidence` 只查附件存在、挂在该任务上；医护端提交 / 办结与居民端提交判 `require_evidence` 只看佐证
清单非空。下载侧（`attachments.download_attachment`）对本行 infected、或同一份内容（sha256）已被隔离的一律 410（P2-395）
——审核人打不开的附件照样让任务办结。实测两种场景都过：上传的内容早被隔离过（同 sha256 当场标 infected），拿它作佐证直接
办结 200；先记进佐证、补扫之后才判毒（异步扫描的常态），清单里只剩这张隔离件，办结照样 200。

修法：`valid_task_evidence` 遇隔离件报「附件 #N 已被病毒扫描隔离」（与下载侧同一判据，复用 `quarantined_copy`）；三处判
`require_evidence` 数的是「未被隔离的佐证」（`platform.usable_task_evidence`），422 与原文案照旧。pending / skipped /
unavailable 照旧放行（旁路定位是可用性优先，见 attachments.py 与 avscan.py）。
"""
import pytest

B = "/api/spd"
P = "/api/portal/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    """一位挂着居民账号的患者（居民端提交用）与一家机构。"""
    from app.config import settings
    from app.database import SessionLocal
    from app.models import Patient, ResidentAccount

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1251 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    with SessionLocal() as db:
        me = Patient(ehc_no="EHC-P1251-ME", name="P1251 居民", id_card="330106198001011251", gender="女",
                     birth_date="1980-01-01", phone="13912511251")
        db.add(me)
        db.flush()
        db.add(ResidentAccount(phone="13912511251", patient_id=me.id, nickname="P1251", wechat_openid="",
                               status="active"))
        db.commit()
        patient_id = me.id
    old = settings.sms_debug_echo
    settings.sms_debug_echo = True
    try:
        code = client.post("/api/portal/auth/sms/code",
                           json={"phone": "13912511251", "purpose": "login"}).json()["debug_code"]
        token = client.post("/api/portal/auth/sms/login",
                            json={"phone": "13912511251", "code": code}).json()["access_token"]
    finally:
        settings.sms_debug_echo = old
    return {"org": org, "patient": patient_id, "resident": {"Authorization": f"Bearer {token}"}}


def _task(client, admin, world, title):
    made = client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": world["patient"], "org_id": world["org"], "title": title, "task_type": "followup",
        "require_evidence": True})
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _upload(client, admin, task_id, content: bytes) -> int:
    upload = client.post("/api/attachments", headers=admin,
                         files={"file": ("evidence.png", b"\x89PNG\r\n\x1a\n" + content, "image/png")},
                         data={"owner_type": "spd_task", "owner_id": str(task_id)})
    assert upload.status_code == 201, upload.text
    return upload.json()["id"]


def _set_scan(attachment_id: int, status: str, detail: str = "") -> None:
    """模拟补扫任务（`avscan.attachment_av_scan`）的结论落库——它写的就是这两列。"""
    from app.database import SessionLocal
    from app.models import Attachment

    with SessionLocal() as db:
        row = db.get(Attachment, attachment_id)
        row.scan_status, row.scan_detail = status, detail
        db.commit()


def _status(client, admin, task_id) -> str:
    return client.get(f"{B}/tasks/{task_id}", headers=admin).json()["status"]


def test_本行已隔离的附件作佐证_提交_办结_记佐证都被拒(client, admin, world):
    task_id = _task(client, admin, world, "P1251 本行隔离")
    att = _upload(client, admin, task_id, b"p1251-own-infected")
    _set_scan(att, "infected", "Eicar-Test-Signature FOUND")
    assert client.get(f"/api/attachments/{att}", headers=admin).status_code == 410   # 下载侧早就不给
    for path, body in (("complete", {"result": {"note": "已随访"}, "evidence": [att]}),
                       ("submit", {"result": {"note": "已随访"}, "evidence": [att]})):
        resp = client.post(f"{B}/tasks/{task_id}/{path}", headers=admin, json=body)
        assert resp.status_code == 422, (path, resp.text)                       # 修前 complete 200 done
        assert resp.json()["detail"] == f"附件 #{att} 已被病毒扫描隔离", (path, resp.text)
    added = client.post(f"{B}/tasks/{task_id}/evidence", headers=admin, json={"attachment_id": att})
    assert added.status_code == 422 and "已被病毒扫描隔离" in added.json()["detail"], added.text
    assert _status(client, admin, task_id) != "done"


def test_同一份内容的另一行已被隔离_这一行作佐证也被拒(client, admin, world):
    """本行没标 infected（早于 P2-395 落的行、或标记还没传到的窗口），同 sha256 的另一行已隔离：下载侧 410，佐证侧同样不认。"""
    first = _task(client, admin, world, "P1251 先传的任务")
    second = _task(client, admin, world, "P1251 后传的任务")
    content = b"p1251-same-sha256"
    infected = _upload(client, admin, first, content)
    _set_scan(infected, "infected", "Eicar-Test-Signature FOUND")
    copy = _upload(client, admin, second, content)
    _set_scan(copy, "clean")                    # 只让「同内容另一行」这条判据起作用
    assert client.get(f"/api/attachments/{copy}", headers=admin).status_code == 410
    resp = client.post(f"{B}/tasks/{second}/complete", headers=admin,
                       json={"result": {"note": "已随访"}, "evidence": [copy]})
    assert resp.status_code == 422 and resp.json()["detail"] == f"附件 #{copy} 已被病毒扫描隔离", resp.text
    assert _status(client, admin, second) != "done"


def test_记进佐证后补扫判毒_清单只剩隔离件_医护提交与办结都被拒(client, admin, world):
    task_id = _task(client, admin, world, "P1251 补扫判毒")
    att = _upload(client, admin, task_id, b"p1251-infected-later")
    added = client.post(f"{B}/tasks/{task_id}/evidence", headers=admin, json={"attachment_id": att})
    assert added.status_code == 200 and added.json()["evidence"] == [att], added.text
    _set_scan(att, "infected", "Eicar-Test-Signature FOUND")   # 记进佐证之后补扫才判出来
    done = client.post(f"{B}/tasks/{task_id}/complete", headers=admin, json={"result": {"note": "已随访"}})
    assert done.status_code == 422, done.text                                  # 修前 200 done
    assert done.json() == {"detail": "该任务要求上传佐证材料后才能办结"}
    submitted = client.post(f"{B}/tasks/{task_id}/submit", headers=admin, json={"result": {"note": "已随访"}})
    assert submitted.status_code == 422, submitted.text                        # 修前 200 submitted
    assert submitted.json() == {"detail": "该任务要求上传佐证材料后才能提交"}
    assert _status(client, admin, task_id) == "doing"
    # 补传一份干净的就能办（被隔离的那条留在清单里，不算数而已）
    clean = _upload(client, admin, task_id, b"p1251-clean-again")
    assert client.post(f"{B}/tasks/{task_id}/evidence", headers=admin, json={"attachment_id": clean}).status_code == 200
    done = client.post(f"{B}/tasks/{task_id}/complete", headers=admin, json={"result": {"note": "已随访"}})
    assert done.status_code == 200 and done.json()["status"] == "done", done.text


def test_居民传的凭证补扫判毒_居民端提交被拒(client, admin, world):
    task_id = _task(client, admin, world, "P1251 居民凭证")
    upload = client.post(f"{P}/tasks/{task_id}/attachments", headers=world["resident"],
                         files={"file": ("report.jpg", b"\xff\xd8\xff\xe0p1251-resident", "image/jpeg")})
    assert upload.status_code == 201, upload.text
    att = upload.json()["attachment_id"]
    assert client.get(f"{B}/tasks/{task_id}", headers=admin).json()["evidence"] == [att]   # 上传即记进佐证（P1-126）
    _set_scan(att, "infected", "Eicar-Test-Signature FOUND")
    resp = client.post(f"{P}/tasks/{task_id}/submit", headers=world["resident"], json={"result": {"note": "已传报告"}})
    assert resp.status_code == 422, resp.text                                   # 修前 200 submitted
    assert resp.json() == {"detail": "该任务需要上传照片或报告等凭证"}
    # 提交时显式带上这张隔离件同样不认
    resp = client.post(f"{P}/tasks/{task_id}/submit", headers=world["resident"],
                       json={"result": {"note": "已传报告"}, "evidence": [att]})
    assert resp.status_code == 422 and resp.json()["detail"] == f"附件 #{att} 已被病毒扫描隔离", resp.text


@pytest.mark.parametrize("scan_status", ["clean", "pending", "skipped", "unavailable"])
def test_干净与未判定的附件照旧放行(client, admin, world, scan_status):
    """只拦已确证隔离的：扫描器慢 / 挂 / 未配置都不该让业务办不了（旁路定位是可用性优先）。"""
    task_id = _task(client, admin, world, f"P1251 放行 {scan_status}")
    att = _upload(client, admin, task_id, f"p1251-pass-{scan_status}".encode())
    _set_scan(att, scan_status)
    done = client.post(f"{B}/tasks/{task_id}/complete", headers=admin,
                       json={"result": {"note": "已随访"}, "evidence": [att]})
    assert done.status_code == 200 and done.json()["status"] == "done", done.text


def test_清单里还有没隔离的_照旧能办结(client, admin, world):
    """数的是「未被隔离的佐证」，不是「一张隔离件就整条拦」：另一份干净的照样满足要佐证。"""
    task_id = _task(client, admin, world, "P1251 混合清单")
    good = _upload(client, admin, task_id, b"p1251-mixed-good")
    bad = _upload(client, admin, task_id, b"p1251-mixed-bad")
    for att in (good, bad):
        assert client.post(f"{B}/tasks/{task_id}/evidence", headers=admin, json={"attachment_id": att}).status_code == 200
    _set_scan(bad, "infected", "Eicar-Test-Signature FOUND")
    submitted = client.post(f"{B}/tasks/{task_id}/submit", headers=admin, json={"result": {"note": "已随访"}})
    assert submitted.status_code == 200 and submitted.json()["status"] == "submitted", submitted.text
