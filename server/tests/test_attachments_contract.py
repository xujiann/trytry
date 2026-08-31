"""通用附件 `/api/attachments` 三个端点的**特征化网 + 响应契约**。

套路同 test_rules_contract.py / test_admin_mgmt_contract.py：先钉住**当前**响应的
完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。

本簇的建模判断：

- 上传回执与列表行是**同一个 `_out()` 形状**（9 键），一个模型两处复用；
  `uploaded_by` 是**键恒在值可空**（居民端上传记 null）→ `int | None`；
  `sha256` 在测试里由同一份字节现算，逐字符钉死。
- 下载端点返回 `FileResponse`/`StreamingResponse` **文件字节流**，`response_model`
  对它没有意义（函数不返回可序列化对象）——照 reports.CsvResponse 的写法给一个
  自带 media_type 的 Response 子类当 `response_class`（本地分支实际返回的就是它）。
  实际 content-type 逐附件回填（image/png、application/pdf……），本文件钉住
  字节体、content-type 与 content-disposition。
- 可见性/角色语义（org 档校验、白名单、magic bytes）一行不动，只顺带钉 415/404。
"""
import hashlib

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

ATTACHMENT_KEYS = [
    "id", "filename", "content_type", "size", "sha256",
    "owner_type", "owner_id", "uploaded_by", "created_at",
]

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"attachment-contract-png".ljust(24, b"0")
PDF_BYTES = b"%PDF-1.4 attachment-contract"


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def event(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "附件契约医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    ev = client.post(
        "/api/quality/adverse-events",
        json={"org_id": org["id"], "event_type": "medication", "level": "III",
              "description": "附件契约佐证"},
        headers=admin,
    )
    assert ev.status_code == 201, ev.text
    return ev.json()


@pytest.fixture(scope="module")
def uploaded(client, admin, event):
    resp = client.post(
        "/api/attachments",
        data={"owner_type": "adverse_event", "owner_id": str(event["id"])},
        files={"file": ("evidence.png", PNG_BYTES, "image/png")},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    pdf = client.post(
        "/api/attachments",
        data={"owner_type": "adverse_event", "owner_id": str(event["id"])},
        files={"file": ("report.pdf", PDF_BYTES, "application/pdf")},
        headers=admin,
    )
    assert pdf.status_code == 201, pdf.text
    return {"png": resp.json(), "pdf": pdf.json()}


def test_上传回执精确形状与键序(event, uploaded):
    body = uploaded["png"]
    assert list(body.keys()) == ATTACHMENT_KEYS
    assert isinstance(body["created_at"], str) and "T" in body["created_at"]
    assert body == {
        "id": body["id"],
        "filename": "evidence.png",
        "content_type": "image/png",
        "size": len(PNG_BYTES),
        "sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
        "owner_type": "adverse_event",
        "owner_id": event["id"],
        "uploaded_by": 1,
        "created_at": body["created_at"],
    }
    assert uploaded["pdf"] == {
        "id": uploaded["pdf"]["id"],
        "filename": "report.pdf",
        "content_type": "application/pdf",
        "size": len(PDF_BYTES),
        "sha256": hashlib.sha256(PDF_BYTES).hexdigest(),
        "owner_type": "adverse_event",
        "owner_id": event["id"],
        "uploaded_by": 1,
        "created_at": uploaded["pdf"]["created_at"],
    }


def test_列举精确_与回执同形(client, admin, event, uploaded):
    rows = client.get(
        f"/api/attachments?owner_type=adverse_event&owner_id={event['id']}", headers=admin
    ).json()
    assert [list(r.keys()) for r in rows] == [ATTACHMENT_KEYS] * 2
    assert rows == [uploaded["png"], uploaded["pdf"]]  # id 正序
    # missing_ok 分支：挂接对象不存在时返回空列表（既有契约，不借治理改掉）
    assert client.get(
        "/api/attachments?owner_type=adverse_event&owner_id=999999", headers=admin
    ).json() == []


def test_下载_文件字节与头精确(client, admin, uploaded):
    resp = client.get(f"/api/attachments/{uploaded['png']['id']}", headers=admin)
    assert resp.status_code == 200
    assert resp.content == PNG_BYTES
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["content-disposition"] == 'attachment; filename="evidence.png"'
    pdf = client.get(f"/api/attachments/{uploaded['pdf']['id']}", headers=admin)
    assert pdf.content == PDF_BYTES and pdf.headers["content-type"] == "application/pdf"
    assert client.get("/api/attachments/999999", headers=admin).status_code == 404


def test_白名单与magic校验语义未动(client, admin, event):
    bad_type = client.post(
        "/api/attachments",
        data={"owner_type": "adverse_event", "owner_id": str(event["id"])},
        files={"file": ("x.txt", b"plain", "text/plain")},
        headers=admin,
    )
    assert bad_type.status_code == 415
    fake_png = client.post(
        "/api/attachments",
        data={"owner_type": "adverse_event", "owner_id": str(event["id"])},
        files={"file": ("x.png", b"<html>not png</html>", "image/png")},
        headers=admin,
    )
    assert fake_png.status_code == 415
    assert client.post(
        "/api/attachments",
        data={"owner_type": "nowhere", "owner_id": "1"},
        files={"file": ("x.png", PNG_BYTES, "image/png")},
        headers=admin,
    ).status_code == 422
