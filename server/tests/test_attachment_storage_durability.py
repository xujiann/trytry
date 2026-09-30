"""附件落盘写到一半失败，半截文件不许留在内容寻址路径上（P2-1112，第三十二批扫描 B1-1）。

修前 `LocalStorage.save` 只有一句 `if not path.exists(): path.write_bytes(data)`：直接写最终路径、不 fsync、也不比大小。
磁盘写满、NFS 抖动、Pod 被杀都会让 write 半途失败——这次上传 500，内容寻址的路径上却留下一个截断文件；之后重传
同一份文件，`save` 见「已存在」就跳过写入，新行 201、回执里的 size 与 sha256 都是完整文件的值，下载拿到的却是半截
（扫描实测 300016 字节的 PDF 下到 150008 字节），此后任何人再传这份文件都挂到这个坏文件上，永远不会自愈。

修后：同目录临时文件独占创建 → 写入 → fsync → `os.replace` 原子换名（与归档任务 `jobs._open_new_archive` /
`jobs._fsync` 同一口径），最终路径上要么没有、要么完整；键已存在而尺寸与来件对不上、来件又确是这个键的内容时原子
重写——存量的半截文件在下一次上传同一内容时被修好。

「写一半失败」用 RLIMIT_FSIZE 在内核层注入：把本进程可写的文件大小压到来件的一半，write 写进一半后 EFBIG，与磁盘
写满同一个现场，也不依赖实现用哪个写文件的 API（修前的 `write_bytes`、修后的临时文件都被同样截在半途）。限额只在
那一次 `save` 期间生效，前后都还原。

变异检查（均实测）：
- 把 `LocalStorage.save` 换回修前那两行 → 两条全红：第一条「下载到半截：150008 / 300016 字节」，第二条「预置的半截
  文件没被修好」；
- 只去掉尺寸核对（保留临时文件换名）→ 第二条红；
- 只去掉临时文件换名（直接写最终路径，保留尺寸核对）→ 第一条红：重传时尺寸核对把文件修好了，但失败的那一次仍在
  最终路径上留下了 150008 字节的半截文件。
"""
import hashlib
import io
import os
import signal

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app
from app.storage import LocalStorage, use_storage

resource = pytest.importorskip("resource")  # RLIMIT_FSIZE 只在 POSIX 上有；Windows 自动旁路

#: 一份 30 万字节的「PDF」：大到一次 write 写不完一半，文件头过得了 `%PDF-` 的类型校验
CONTENT = b"%PDF-1.4\n" + os.urandom(300_000) + b"\n%%EOF\n"
SIZE = len(CONTENT)
SHA = hashlib.sha256(CONTENT).hexdigest()


class _HalfWriteOnce(LocalStorage):
    """第一次 `save` 时把本进程可写的文件大小压到来件的一半：内核写进一半后 EFBIG（磁盘写满同一个现场）。"""

    def __init__(self, root):
        super().__init__(root)
        self.armed = True

    def save(self, key, data):
        if not self.armed:
            return super().save(key, data)
        self.armed = False
        soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        resource.setrlimit(resource.RLIMIT_FSIZE, (len(data) // 2, hard))
        try:
            return super().save(key, data)
        finally:
            resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))


@pytest.fixture(scope="module")
def client():
    """本地 client：写盘失败要能看到 500 本身（共享版 raise_server_exceptions=True 会把异常直接抛进用例）。"""
    reset_database()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def event(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21112 医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    resp = client.post("/api/quality/adverse-events", headers=admin, json={
        "org_id": org, "event_type": "device", "level": "III", "description": "P21112 监护仪截图"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture()
def xfsz_ignored():
    """超限写默认是 SIGXFSZ 杀进程；忽略它，write 才会像磁盘写满那样返回错误。signal 只能在主线程装，所以放在用例这一侧。"""
    previous = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    yield
    signal.signal(signal.SIGXFSZ, previous)


@pytest.fixture()
def storage_at(tmp_path):
    """把附件存储换成 tmp_path 下的本地后端（经正式的装配入口 `use_storage`），用例结束还原。"""
    def _use(backend):
        use_storage(backend)
        return backend

    yield _use
    use_storage(None)


def _upload(client, admin, event):
    return client.post("/api/attachments", headers=admin,
                       data={"owner_type": "adverse_event", "owner_id": str(event)},
                       files={"file": ("监护仪截图.pdf", io.BytesIO(CONTENT), "application/pdf")})


def test_写到一半失败后重传同一文件_下载字节与原文件一致(client, admin, event, xfsz_ignored, storage_at, tmp_path):
    storage = storage_at(_HalfWriteOnce(tmp_path))

    first = _upload(client, admin, event)
    assert first.status_code == 500, first.text          # 磁盘写满：这一次照实失败
    bucket = tmp_path / SHA[:2]
    leftovers = {p.name: p.stat().st_size for p in bucket.iterdir()}
    existed_after_failure = storage.exists(SHA)

    second = _upload(client, admin, event)                # 空间恢复后经办重传同一份文件
    assert second.status_code == 201, second.text
    assert second.json()["size"] == SIZE and second.json()["sha256"] == SHA
    down = client.get(f"/api/attachments/{second.json()['id']}", headers=admin)
    assert down.status_code == 200, down.text
    got = len(down.content)
    assert got == SIZE, f"下载到半截：{got} / {SIZE} 字节"   # 修前 150008 / 300016
    assert down.content == CONTENT
    # 失败的那一次在桶里什么都不留：最终路径上没有半截文件，临时文件也清掉了
    assert not existed_after_failure and leftovers == {}, f"失败的上传在桶里留下了：{leftovers}"


def test_存量半截文件_再传同一内容时被修好(client, admin, event, storage_at, tmp_path):
    """修前已经留在盘上的半截文件：下一次有人传这份内容就该把它修好，而不是永远挂着它。"""
    storage_at(LocalStorage(tmp_path))
    truncated = tmp_path / SHA[:2] / SHA
    truncated.parent.mkdir(parents=True)
    truncated.write_bytes(CONTENT[: SIZE // 2])

    up = _upload(client, admin, event)
    assert up.status_code == 201, up.text
    assert truncated.read_bytes() == CONTENT, (
        f"预置的半截文件没被修好：盘上 {truncated.stat().st_size} / 原文件 {SIZE} 字节")
    down = client.get(f"/api/attachments/{up.json()['id']}", headers=admin)
    assert down.status_code == 200, down.text
    assert down.content == CONTENT
    assert [p.name for p in truncated.parent.iterdir()] == [SHA]   # 换名之后不留临时文件
