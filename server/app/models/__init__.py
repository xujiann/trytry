"""数据模型包（原单文件 app/models.py 近 4000 行拆分而来）。

按开发阶段/领域切成 core / extended / management / platform 四块 + _shared 前导，
`__init__` 全量重导出，`from app.models import X` 与 `from ..models import X` 一律不变。
拆分只挪行不改类：跨块外键/关系是字符串，由 SQLAlchemy registry 解析，与文件边界无关。"""
from ._shared import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .extended import *  # noqa: F401,F403
from .management import *  # noqa: F401,F403
from .platform import *  # noqa: F401,F403
