"""A5 附件存储抽象：协议行为单测 + 与原内联实现的磁盘布局兼容性。

守的点：
1. LocalStorage 的 save/exists/open/delete/local_path 语义（含幂等）；
2. 磁盘布局与抽象前**逐字节一致**（``{root}/{sha256前2位}/{sha256}`` 两级分桶）——
   已上线环境磁盘上的存量附件必须不迁移即可读；
3. get_storage/use_storage 的装配语义：默认取 settings.upload_dir 的本地后端，
   注册的替代后端（对象存储等）优先，传 None 恢复默认；
4. attachments 路由不再绕过抽象直接摸磁盘（AST 防复发）。
"""
import ast
import hashlib
from pathlib import Path

import pytest

from app import storage as storage_mod
from app.storage import LocalStorage, get_storage, use_storage


@pytest.fixture()
def local(tmp_path):
    return LocalStorage(tmp_path)


def _key(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_save_exists_open_roundtrip(local):
    data = b"%PDF-1.4 storage roundtrip"
    key = _key(data)
    assert not local.exists(key)
    local.save(key, data)
    assert local.exists(key)
    with local.open(key) as f:
        assert f.read() == data


def test_save_is_idempotent_and_content_addressed(local, tmp_path):
    """同键重复 save 不重写——内容寻址下同键必同内容，保持原实现的去重语义。"""
    data = b"same bytes"
    key = _key(data)
    local.save(key, data)
    path = tmp_path / key[:2] / key
    before = path.stat().st_mtime_ns
    local.save(key, b"attacker tries to overwrite")  # 已存在：应跳过，不覆盖
    assert path.read_bytes() == data
    assert path.stat().st_mtime_ns == before


def test_disk_layout_matches_legacy_bucketing(local, tmp_path):
    """磁盘布局必须与抽象前一致：{root}/{sha256[:2]}/{sha256}，存量文件免迁移。"""
    data = b"legacy layout check"
    key = _key(data)
    local.save(key, data)
    assert (tmp_path / key[:2] / key).read_bytes() == data
    assert local.local_path(key) == tmp_path / key[:2] / key


def test_legacy_files_readable_without_migration(local, tmp_path):
    """反向：按旧实现手工落盘的文件，新后端不迁移即能 exists/open。"""
    data = b"written by the pre-A5 inline code"
    key = _key(data)
    bucket = tmp_path / key[:2]
    bucket.mkdir(parents=True)
    (bucket / key).write_bytes(data)
    assert local.exists(key)
    with local.open(key) as f:
        assert f.read() == data


def test_delete_removes_and_is_idempotent(local):
    data = b"to be deleted"
    key = _key(data)
    local.save(key, data)
    local.delete(key)
    assert not local.exists(key)
    local.delete(key)  # 不存在时静默返回，不抛
    assert not local.exists(key)


def test_open_missing_key_raises(local):
    with pytest.raises(FileNotFoundError):
        local.open(_key(b"never saved"))


def test_get_storage_defaults_to_local_under_upload_dir():
    """默认后端：本地磁盘，根目录取 settings.upload_dir（conftest 指到 ./test_uploads）。"""
    from app.config import settings

    backend = get_storage()
    assert isinstance(backend, LocalStorage)
    assert backend.root == Path(settings.upload_dir)


def test_use_storage_override_and_reset(local):
    """装配注册的替代后端优先；传 None 恢复默认本地后端。"""
    try:
        use_storage(local)
        assert get_storage() is local
    finally:
        use_storage(None)
    assert isinstance(get_storage(), LocalStorage)
    assert get_storage() is not local


def test_attachments_router_does_not_touch_disk_directly():
    """AST 防复发：附件路由只准经 app.storage 存取字节。

    抽象的意义在于「路由不知道字节在哪」；若谁又在路由里写回
    Path(settings.upload_dir) / write_bytes / open 一类直连磁盘的代码，
    多实例 404 的坑就会回来。这里扫 AST 拦住。
    """
    src = (Path(__file__).parent.parent / "app" / "routers" / "attachments.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in (
            "upload_dir", "write_bytes", "read_bytes", "unlink", "mkdir",
        ):
            offenders.append(f"line {node.lineno}: .{node.attr}")
        if isinstance(node, ast.Name) and node.id == "Path":
            offenders.append(f"line {node.lineno}: Path(...)")
    assert not offenders, (
        f"attachments.py 出现直连磁盘的痕迹：{offenders}；字节存取请走 app.storage"
    )


def test_storage_docstring_states_shared_volume_requirement():
    """部署红线要写在离代码最近处：多实例 + 本地后端必须共享卷。"""
    doc = storage_mod.__doc__ or ""
    assert "共享卷" in doc and "多实例" in doc
