"""全域慢专病 · 配置域：病种规则、管理目标、标准路径、量表、宣教、服务包、
团队与村医、设备、数据源、专病中心。

对应招标文件：平台管理端 #2~#17、专病专家端 #1~#3、卫健管理端 #5~#8。

配置写接口统一收在 `require_roles("director")`（admin 自动通过）而不是 `require_admin`：
专病中心的牵头科室主任要能改自己病种的路径与目标，全部压到平台管理员会让配置
变成一件"要提工单"的事，实施期没人受得了。真正的平台级开关（病种启停、
数据源接入）仍然要 admin。

本模块原本是单文件 1549 行，按业务分节拆成了包（ADR-0008）。

**导入路径不变**：外部仍然 `from .routers import config` 再用 `config.router`，
拆包对调用方是透明的。

子模块的导入顺序**必须与原文件的分节顺序一致**——路由是在 import 时通过装饰器
注册到同一个 `router` 上的，顺序决定 FastAPI 的匹配优先级。乱序不会报错，
只会让某些路径悄悄匹配到别的处理函数上。
"""
from ._base import router

# 顺序即注册顺序，勿随手调整（理由见上）
from . import catalog, paths, scales, teams, devices, centers  # noqa: E402,F401

__all__ = ["router"]
