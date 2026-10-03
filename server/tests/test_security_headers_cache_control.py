"""接口响应一律 `Cache-Control: no-store`（P2-1215，第三十五批扫描 T1-2）。

修前全站响应都不带 Cache-Control。附件下载走 FileResponse（`routers/attachments.py`），带 Last-Modified / ETag，浏览器
按启发式新鲜度（文件年龄的 10%）直接用磁盘缓存：甲医生第二次下载到不了服务端——不判可见性、不留 AccessLog；甲退出、
与该患者毫无关系的乙卫生院医生登录（列附件 403），fetch 同一地址照样从缓存取回整份 PDF，服务端命中与留痕都不变
（scan35 t1/r2，真 Chromium 实测）；附件事后被病毒扫描隔离回 410，缓存里的副本照样能取。档案、打印页这些带证件号 /
诊断的响应同样没有 no-store，留在共用电脑的磁盘缓存里。

修法：`main.py` 的安全头中间件给 `/api/` 开头的响应统一 setdefault `Cache-Control: no-store`（端点自设的不覆盖，眼下
没有这样的端点），不改任何响应体。本文件钉住附件下载两支（本地 FileResponse、对象存储流式）、清单 / 详情 / 档案 /
导出 CSV / 打印页 / 登录，以及 401 / 403 / 404 / 422 错误响应都带 no-store；入口页与 /static 不归这一条。
"""
import io

import pytest

from app.storage import LocalStorage, use_storage
from conftest import login

PDF = b"%PDF-1.4 P2-1215 renal report"


class _ObjectLikeStorage:
    """借本地目录存字节，但声称没有本地路径——下载因此走流式那一支（同 test_attachment_download_object_storage）。"""

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


@pytest.fixture(scope="module")
def world(client, admin):
    """卫生院医师上转一位患者并给转诊单挂一份 PDF；另一个镇的医师与该患者毫无关系。"""
    county = client.post("/api/organizations", headers=admin, json={
        "name": "缓存头甲院", "org_type": "lead_hospital", "level": "county"}).json()
    township = client.post("/api/organizations", headers=admin, json={
        "name": "缓存头卫生院", "org_type": "township", "level": "township", "parent_id": county["id"]}).json()
    other = client.post("/api/organizations", headers=admin, json={
        "name": "缓存头乙镇卫生院", "org_type": "township", "level": "township"}).json()
    patient = client.post("/api/patients", headers=admin, json={
        "name": "缓存头患者", "id_card": "330281199007078019", "gender": "女",
        "birth_date": "1990-07-07", "phone": "13887654321"}).json()
    for username, org_id in (("cc_doc_t", township["id"]), ("cc_doc_b", other["id"])):
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pass123456", "role": "doctor", "org_id": org_id})
        assert resp.status_code in (200, 201), resp.text
    doc_t = login(client, "cc_doc_t", "pass123456")
    referral = client.post("/api/referrals", headers=doc_t, json={
        "patient_id": patient["id"], "from_org_id": township["id"], "to_org_id": county["id"],
        "direction": "up", "reason": "缓存头回归：肾功能恶化"})
    assert referral.status_code == 201, referral.text
    up = client.post("/api/attachments", headers=doc_t,
                     data={"owner_type": "referral", "owner_id": str(referral.json()["id"])},
                     files={"file": ("肾功能报告.pdf", io.BytesIO(PDF), "application/pdf")})
    assert up.status_code == 201, up.text
    return {
        "admin": admin, "doc_t": doc_t, "doc_b": login(client, "cc_doc_b", "pass123456"), "anon": {},
        "ehc_no": patient["ehc_no"], "referral_id": referral.json()["id"],
        "attachment_id": up.json()["id"],
    }


@pytest.fixture()
def object_storage(tmp_path):
    use_storage(_ObjectLikeStorage(LocalStorage(tmp_path)))
    yield
    use_storage(None)


def test_附件下载_本地存储一支带no_store(client, world):
    resp = client.get(f"/api/attachments/{world['attachment_id']}", headers=world["doc_t"])
    assert resp.status_code == 200, resp.text
    assert resp.content == PDF
    # 修前这里没有 Cache-Control、却带着 Last-Modified：浏览器按启发式新鲜度直接用缓存，下一位登录的人也取得到
    assert resp.headers.get("cache-control") == "no-store", dict(resp.headers)


def test_附件下载_对象存储流式一支带no_store(client, world, object_storage):
    """对象存储等非本地后端走 StreamingResponse；先在这个后端里存一份，再下载。"""
    up = client.post("/api/attachments", headers=world["doc_t"],
                     data={"owner_type": "referral", "owner_id": str(world["referral_id"])},
                     files={"file": ("复查报告.pdf", io.BytesIO(PDF + b" object"), "application/pdf")})
    assert up.status_code == 201, up.text
    resp = client.get(f"/api/attachments/{up.json()['id']}", headers=world["doc_t"])
    assert resp.status_code == 200, resp.text
    assert resp.content == PDF + b" object"
    assert resp.headers.get("cache-control") == "no-store", dict(resp.headers)


@pytest.mark.parametrize("who,path,status", [
    pytest.param("doc_b", "/api/attachments/{attachment_id}", 403, id="附件下载-无权403"),
    pytest.param("doc_t", "/api/attachments/999999", 404, id="附件下载-不存在404"),
    pytest.param("doc_t", "/api/attachments?owner_type=referral&owner_id={referral_id}", 200, id="附件清单"),
    pytest.param("admin", "/api/patients", 200, id="患者清单"),
    pytest.param("admin", "/api/patients/{ehc_no}", 200, id="患者详情"),
    pytest.param("admin", "/api/archive/{ehc_no}", 200, id="健康档案"),
    pytest.param("admin", "/api/reports/monitoring/export", 200, id="导出CSV"),
    pytest.param("admin", "/api/print/referrals/{referral_id}", 200, id="打印页"),
    pytest.param("anon", "/api/health", 200, id="健康检查"),
    pytest.param("anon", "/api/patients", 401, id="未登录401"),
    pytest.param("doc_t", "/api/reports/monitoring/export", 403, id="角色不符403"),
    pytest.param("doc_t", "/api/attachments?owner_type=referral", 422, id="缺参数422"),
    pytest.param("admin", "/api/print/referrals/999999", 404, id="打印页-不存在404"),
])
def test_接口响应一律no_store(client, world, who, path, status):
    resp = client.get(path.format(**world), headers=world[who])
    assert resp.status_code == status, resp.text[:300]
    assert resp.headers.get("cache-control") == "no-store", dict(resp.headers)


def test_登录响应也带no_store(client, world):
    """响应体里就是访问令牌。"""
    resp = client.post("/api/auth/login", json={"username": "cc_doc_t", "password": "pass123456"})
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("cache-control") == "no-store", dict(resp.headers)


@pytest.mark.parametrize("path", ["/", "/m", "/m/doctor", "/verify", "/static/core.js", "/static/m/m.js"])
def test_入口页与静态资源不归这一条(client, path):
    """入口页与 /static 不带患者数据，不该被一并禁存（它们要靠 ETag 重验省流量）。"""
    resp = client.get(path)
    assert resp.status_code == 200
    assert "no-store" not in resp.headers.get("cache-control", ""), dict(resp.headers)
