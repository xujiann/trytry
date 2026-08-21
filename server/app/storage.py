"""附件存储抽象（A5）：把「附件字节放在哪」从路由里抽出来。

现状与动机
----------
附件路由此前直接读写 ``settings.upload_dir`` 本地目录。单实例没问题；一旦多实例
部署（compose 副本、K8s 多 Pod），上传落在 A 实例的磁盘、下载打到 B 实例，就是 404。
本模块定义一个**极简存储协议**（按 attachments 的实际用法提炼，不多不少），
路由只面向协议编程，后端可替换。

协议（`Storage`）
----------------
键（key）就是附件内容的 sha256 十六进制串——attachments 一直按内容寻址去重，
协议沿用这一语义，键由调用方给出，存储层不关心其含义。

- ``save(key, data)``   —— 幂等写入：键已存在时不重写（内容寻址，同键必同内容）
- ``exists(key)``       —— 键下是否有内容
- ``open(key)``         —— 以二进制只读方式打开，返回 file-like（调用方负责 close）
- ``delete(key)``       —— 删除；键不存在时静默返回（幂等）
- ``local_path(key)``   —— 本地后端返回磁盘路径（供 FileResponse 零拷贝回源）；
                            非本地后端返回 ``None``，路由退回 ``open()`` 流式响应

部署注意（重要）
----------------
- **本地后端（LocalStorage）多实例部署时，`MEDPLAT_UPLOAD_DIR` 必须指向共享卷**
  （NFS / 云盘 / 云文件存储），否则各实例各存各的，上传与下载会 404。
  单实例（含 docker-compose 单副本 + 命名卷）不受影响。
- **对象存储后端（S3/OSS/COS 等）按协议实现后经 `use_storage()` 注册**：
  在启动装配处（而非本模块）import 对应 SDK、实现上面五个方法、调用
  ``use_storage(MyObjectStorage(...))`` 即可，路由代码零改动。
  本仓库**不预装任何对象存储 SDK**（CLAUDE.md 第 12 条：不无理由引入新依赖），
  谁接对象存储谁在自己的部署层带上依赖。

行为兼容
--------
LocalStorage 与原 attachments 内联实现**逐字节一致**：目录仍是
``{upload_dir}/{sha256前2位}/{sha256}`` 两级分桶，磁盘上已有的附件无需迁移。
"""
from pathlib import Path
from typing import BinaryIO, Protocol

from .config import settings


class Storage(Protocol):
    """附件字节的存取协议。键=内容 sha256，语义见模块 docstring。"""

    def save(self, key: str, data: bytes) -> None: ...

    def exists(self, key: str) -> bool: ...

    def open(self, key: str) -> BinaryIO: ...

    def delete(self, key: str) -> None: ...

    def local_path(self, key: str) -> Path | None: ...


class LocalStorage:
    """本地磁盘后端：与原 attachments 内联实现逐字节一致（两级 sha256 分桶）。

    多实例部署必须把根目录放在共享卷上（见模块 docstring）。
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        """按 key 前2位分桶。目录按需创建——与原 `_upload_root`/`_stored_path` 相同。"""
        bucket = self.root / key[:2]
        bucket.mkdir(parents=True, exist_ok=True)
        return bucket / key

    def save(self, key: str, data: bytes) -> None:
        path = self._path(key)
        if not path.exists():  # 内容寻址：同键必同内容，已有即跳过（保持原幂等语义）
            path.write_bytes(data)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def open(self, key: str) -> BinaryIO:
        return self._path(key).open("rb")

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def local_path(self, key: str) -> Path | None:
        return self._path(key)


#: 装配期注册的替代后端（如对象存储）。None = 用默认的 LocalStorage。
_override: Storage | None = None


def use_storage(storage: Storage | None) -> None:
    """注册替代后端；传 None 恢复默认本地后端。在启动装配处调用一次即可。"""
    global _override
    _override = storage


def get_storage() -> Storage:
    """取当前存储后端。

    默认后端每次现取 ``settings.upload_dir``（不在 import 期缓存路径）——
    与原实现一致，测试环境在 conftest 里改 `MEDPLAT_UPLOAD_DIR` 才能生效。
    """
    if _override is not None:
        return _override
    return LocalStorage(settings.upload_dir)
