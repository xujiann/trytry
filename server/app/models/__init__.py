"""数据模型包（原单文件 app/models.py 近 4000 行拆分而来）。

按开发阶段/领域切成 core / extended / management / platform 四块 + _shared 前导，
`__init__` 全量重导出，`from app.models import X` 与 `from ..models import X` 一律不变。
拆分只挪行不改类：跨块外键/关系是字符串，由 SQLAlchemy registry 解析，与文件边界无关。"""
from ._shared import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .extended import *  # noqa: F401,F403
from .management import *  # noqa: F401,F403
from .platform import *  # noqa: F401,F403

# 第十四轮 E1–E9 功能增强新增领域模型（各自独立文件，此处集中重导出）
from .enhance_contract import *  # noqa: F401,F403  E1 家医签约内容化
from .enhance_referral import *  # noqa: F401,F403  E3 转诊闭环
from .enhance_pubhealth import *  # noqa: F401,F403  E4 公卫短板
from .enhance_supply import *  # noqa: F401,F403  E5 统一采购配送
from .enhance_intel import *  # noqa: F401,F403  E6 数据智能层
from .enhance_payment import *  # noqa: F401,F403  E2 DRG/DIP 结算+智能审核
from .enhance_cdss import *  # noqa: F401,F403  E7 临床决策支持
from .enhance_portal import *  # noqa: F401,F403  E8 居民端可办
from .enhance_interop import *  # noqa: F401,F403  E9 互操作标准化
