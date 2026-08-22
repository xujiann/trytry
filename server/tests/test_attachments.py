"""块2：通用附件能力——上传/下载/按owner查询/角色越权403/超限413/类型白名单415。"""
import io
import shutil

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c
    shutil.rmtree("./test_uploads", ignore_errors=True)


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def setup(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "附件测试医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "附件测试患者", "id_card": "330281199101016006"},
        headers=admin,
    ).json()
    users = {}
    for username, role in [("att_doc", "doctor"), ("att_ph", "public_health"), ("att_op", "operator")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
        users[role] = login(client, username, "pass123456")
    # 检查报告：开单 → 领取 → 出报告
    req = client.post(
        "/api/exams",
        json={
            "patient_id": patient["id"],
            "from_org_id": org["id"],
            "center_type": "imaging",
            "item_code": "CT-CHEST",
            "item_name": "胸部CT平扫",
        },
        headers=users["doctor"],
    ).json()
    client.post(f"/api/exams/{req['id']}/claim", headers=users["doctor"])
    report = client.post(
        f"/api/exams/{req['id']}/report",
        json={"conclusion": "右肺上叶结节，建议随访", "critical": False},
        headers=users["doctor"],
    ).json()
    # 不良事件
    event = client.post(
        "/api/quality/adverse-events",
        json={"org_id": org["id"], "event_type": "device", "level": "III", "description": "输液泵报警失灵"},
        headers=users["operator"],
    ).json()
    return {"org": org, "patient": patient, "report": report, "event": event, **users}


def _upload(client, headers, owner_type, owner_id, filename, content, content_type):
    return client.post(
        "/api/attachments",
        data={"owner_type": owner_type, "owner_id": str(owner_id)},
        files={"file": (filename, io.BytesIO(content), content_type)},
        headers=headers,
    )


def test_upload_download_exam_report_attachment(client, setup):
    content = b"%PDF-1.4 fake exam report pdf bytes"
    resp = _upload(
        client, setup["doctor"], "exam_report", setup["report"]["id"],
        "chest_ct.pdf", content, "application/pdf",
    )
    assert resp.status_code == 201, resp.text
    meta = resp.json()
    assert meta["filename"] == "chest_ct.pdf"
    assert meta["size"] == len(content)
    assert len(meta["sha256"]) == 64
    # 鉴权下载：内容与类型一致
    download = client.get(f"/api/attachments/{meta['id']}", headers=setup["operator"])
    assert download.status_code == 200
    assert download.content == content
    assert download.headers["content-type"].startswith("application/pdf")
    # 未登录不可下载
    assert client.get(f"/api/attachments/{meta['id']}").status_code == 401


def test_upload_image_and_query_by_owner(client, setup):
    png = b"\x89PNG\r\n\x1a\n" + b"fakepngdata" * 10
    resp = _upload(
        client, setup["operator"], "adverse_event", setup["event"]["id"],
        "scene.png", png, "image/png",
    )
    assert resp.status_code == 201, resp.text
    listed = client.get(
        f"/api/attachments?owner_type=adverse_event&owner_id={setup['event']['id']}",
        headers=setup["public_health"],
    ).json()
    assert len(listed) == 1 and listed[0]["filename"] == "scene.png"
    # 其他对象无附件
    empty = client.get(
        "/api/attachments?owner_type=adverse_event&owner_id=99999", headers=setup["doctor"]
    ).json()
    assert empty == []


def test_upload_forbidden_role_403(client, setup):
    """越权：公卫人员不在检查报告附件上传角色白名单内。"""
    resp = _upload(
        client, setup["public_health"], "exam_report", setup["report"]["id"],
        "sneak.png", b"\x89PNGxx", "image/png",
    )
    assert resp.status_code == 403


def test_upload_oversize_413(client, setup):
    big = b"x" * (10 * 1024 * 1024 + 1)
    resp = _upload(
        client, setup["doctor"], "exam_report", setup["report"]["id"],
        "huge.pdf", big, "application/pdf",
    )
    assert resp.status_code == 413


def test_upload_bad_type_and_bad_owner(client, setup):
    # 类型白名单外 415
    assert _upload(
        client, setup["doctor"], "exam_report", setup["report"]["id"],
        "notes.txt", b"plain text", "text/plain",
    ).status_code == 415
    # 未知业务域 422
    assert _upload(
        client, setup["doctor"], "unknown_owner", 1, "a.png", b"\x89PNG", "image/png"
    ).status_code == 422
    # 业务对象不存在 404
    assert _upload(
        client, setup["doctor"], "exam_report", 99999, "a.png", b"\x89PNG", "image/png"
    ).status_code == 404


def test_magic_bytes_内容与声明类型不一致415(client, setup):
    """Content-Type 是调用方自报的：此前一段 HTML 自称 image/png 就能混进来，
    下载时又按我们回填的 content-type 原样奉还——存储型 XSS 的现成载体。
    现在读文件头校验，声明与内容不一致按 415 拒。"""
    cases = [
        ("fake.png", b"<html><script>alert(1)</script></html>", "image/png"),
        ("fake.pdf", b"<html>not a pdf</html>", "application/pdf"),
        ("fake.jpg", b"GIF89a-actually-a-gif", "image/jpeg"),
        ("fake.gif", b"\x89PNG\r\n\x1a\n-actually-a-png", "image/gif"),
        ("fake.webp", b"RIFF\x00\x00\x00\x00NOPE", "image/webp"),
        # 截断的签名也不算数：前缀像但不全
        ("trunc.png", b"\x89PNGxx", "image/png"),
    ]
    for filename, content, content_type in cases:
        resp = _upload(
            client, setup["doctor"], "exam_report", setup["report"]["id"],
            filename, content, content_type,
        )
        assert resp.status_code == 415, f"{filename} 应 415，实际 {resp.status_code}: {resp.text[:80]}"


def test_magic_bytes_真实文件头照常放行(client, setup):
    """反向断言：校验不能把真文件挡掉——白名单五种类型逐一放行。"""
    cases = [
        ("ok.png", b"\x89PNG\r\n\x1a\n" + b"x" * 16, "image/png"),
        ("ok.jpg", b"\xff\xd8\xff\xe0" + b"x" * 16, "image/jpeg"),
        ("ok.gif", b"GIF89a" + b"x" * 16, "image/gif"),
        ("ok.webp", b"RIFF\x28\x00\x00\x00WEBPVP8 " + b"x" * 8, "image/webp"),
        ("ok.pdf", b"%PDF-1.7\n%minimal", "application/pdf"),
    ]
    for filename, content, content_type in cases:
        resp = _upload(
            client, setup["doctor"], "exam_report", setup["report"]["id"],
            filename, content, content_type,
        )
        assert resp.status_code == 201, f"{filename} 被误拦：{resp.status_code} {resp.text[:80]}"
