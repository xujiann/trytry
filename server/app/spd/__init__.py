"""全域慢专病全流程管理系统 —— 县域医共体信息化平台的**子系统**。

依据招标文件《服务内容及要求》十一端 163 条建设，逐条对照见
`docs/全域慢专病全流程管理系统_需求对照表.md`；边界与集成契约见
`docs/慢专病子系统架构说明.md`。

## 它是子系统，不是独立系统

依赖方向是**单向**的：慢专病依赖平台，平台不依赖慢专病。平台侧只有两处触点，
都在本文件收口：

1. `app/models.py` 末尾 import 本包的模型，让 `Base.metadata` 认识这批表；
2. `app/main.py` 调 `register_spd(app)` 注册路由与种子。

除此之外，平台任何模块都不得 import 本包——`tests/test_spd_boundary.py` 会失败。
反向的依赖（本包 → 平台）一律经 `platform.py`，那里有可数的白名单。

## 装卸

`MEDPLAT_SPD_ENABLED=false` 时不注册路由、不做种子化。表结构仍在（迁移是全量的），
但没有任何入口——这样"只买基础平台的县"不会在菜单里看到用不上的功能，
也不必维护一套单独的构建。
"""
from fastapi import FastAPI

from ..config import settings

#: 子系统对外提供的接口前缀。边界用例据此校验"路由不许长在别处"。
API_PREFIXES = ("/api/spd", "/api/portal/spd")

#: 子系统全部数据表的前缀。运维按前缀做备份、迁移与库内授权。
TABLE_PREFIX = "spd_"


def spd_enabled() -> bool:
    return settings.spd_enabled


def register_spd(app: FastAPI) -> int:
    """把子系统装进平台应用；返回注册的路由模块数（未启用返回 0）。

    做成函数而不是在 `main.py` 里排九行 `include_router`：装卸是一个动作，
    散在九行里就会出现"关掉子系统时漏关了一个路由"。
    """
    if not spd_enabled():
        return 0
    from .routers import (
        assess, care, config, followup, population, portal, referral, tasks, workbench,
    )
    from . import jobs as _jobs  # noqa: F401 - 导入即完成定时任务注册
    from .subscribers import register_subscribers

    from .models import SpdTask
    from .platform import register_attachment_owner

    modules = (config, population, tasks, care, referral, assess, followup, workbench, portal)
    for module in modules:
        app.include_router(module.router)
    # 任务佐证材料挂接平台附件服务（注册制：平台不 import 子系统，方向不反转）
    # 任务佐证材料是患者数据（SpdTask 直接挂 patient_id），走患者可见性 + 留痕
    register_attachment_owner(
        "spd_task", SpdTask, ("doctor", "public_health", "director", "operator"),
        "patient", patient_of=lambda db, task: task.patient_id,
    )
    # 订阅平台领域事件（出院、就诊）与注册定时任务。和路由一起装卸：
    # 子系统关掉就不再听、也不再跑，否则会出现"菜单里没有这个功能，
    # 但出院时它还在写数据"。
    register_subscribers()
    return len(modules)


def seed_spd(db) -> bool:
    """种子化子系统基础配置；未启用时不落任何数据，返回是否执行。"""
    if not spd_enabled():
        return False
    from .seed import seed_all

    seed_all(db)
    return True
