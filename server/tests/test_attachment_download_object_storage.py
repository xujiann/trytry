"""对象存储后端下载中文文件名的附件 500（P2-237）。

附件下载有两支：本地存储走 `FileResponse`，对象存储等非本地后端走 `StreamingResponse` 流式回源，注释写着「header
口径与 FileResponse 一致」。可流式那一支直接把文件名塞进 `filename="…"`：响应头按 latin-1 编码，「检查报告.pdf」
这种中文文件名编不进去，下载即 500（`UnicodeEncodeError`）。FileResponse 对非 ASCII 文件名早就改用 RFC 5987 的
`filename*=utf-8''…`。开发与测试都用本地存储，这一支平时走不到；这里注册一个「没有本地路径」的后端把它走一遍。

修法：抽 `_content_disposition`，与 FileResponse 同一套写法。
"""
import io
from urllib.parse import quote

import pytest

from app.storage import LocalStorage, use_storage


class _ObjectLikeStorage:
    """借本地目录存字节，但声称没有本地路径——下载因此走流式那一支（与接对象存储时一样）。"""

    def __init__(self, inner: LocalStorage):
        self.inner = inner

    def save(self, key, data):
        self.inner.save(key, data)

    def exists(self, key):
        return self.inner.exists(key)

    def open(self, key):
        return self.inner.open(key)

    def local_path(self, key):
        return None


@pytest.fixture()
def object_storage(tmp_path):
    use_storage(_ObjectLikeStorage(LocalStorage(tmp_path)))
    yield
    use_storage(None)


@pytest.fixture(scope="module")
def event(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2237 医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    resp = client.post("/api/quality/adverse-events", headers=admin, json={
        "org_id": org, "event_type": "device", "level": "III", "description": "P2237 输液泵报警失灵"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.parametrize("filename", ["检查报告.pdf", "report.pdf"])
def test_对象存储下载_中文与英文文件名都照常下载(client, admin, event, object_storage, filename):
    content = b"%PDF-1.4 P2237 " + filename.encode()
    up = client.post("/api/attachments", headers=admin, data={"owner_type": "adverse_event", "owner_id": str(event)},
                     files={"file": (filename, io.BytesIO(content), "application/pdf")})
    assert up.status_code == 201, up.text
    down = client.get(f"/api/attachments/{up.json()['id']}", headers=admin)
    assert down.status_code == 200, down.text        # 修前中文文件名 500
    assert down.content == content
    expected = (f'attachment; filename="{filename}"' if filename.isascii()
                else f"attachment; filename*=utf-8''{quote(filename)}")
    assert down.headers["content-disposition"] == expected
