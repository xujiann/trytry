"""慢专病子系统的边界守卫。

这套用例守的不是某个功能，是**结构**：子系统与平台之间的依赖方向、依赖面、
表名与路由的归属。理由与仓库里既有的越权矩阵、并发扫描一致——

> 依赖方向一旦反转就再也回不去了，要在它发生的那一刻失败，而不是半年后
> 在"为什么改一下慢专病，医保结算跑不起来了"里发现。

边界的口径写在 `app/spd/platform.py` 与 `docs/慢专病子系统架构说明.md`，
本文件是那份口径的可执行版本。
"""
import ast
import os

import pytest
from fastapi import FastAPI

from app.spd import API_PREFIXES, TABLE_PREFIX, register_spd

APP_DIR = os.path.join(os.path.dirname(__file__), "..", "app")
SPD_DIR = os.path.join(APP_DIR, "spd")

#: 子系统允许依赖的平台模块。清单之外的依赖要先在这里加一行并说明理由——
#: 依赖面必须始终是可数的，否则"子系统"只是一个目录名。
PLATFORM_ALLOWLIST = {
    "config",        # 装卸开关等配置项
    "clock",         # 统一的 naive UTC 时间源
    "database",      # 会话与 Base
    "datetypes",     # 日期入参类型（真日历校验）
    "deps",          # 鉴权、分页、角色守卫、业务日期
    "visibility",    # 横向数据隔离（机构可见性、患者可见性与留痕）
    "concurrency",   # 原子累加/扣减、唯一约束冲突助手
    "formula",       # AST 白名单表达式求值（考核公式）
    "events",        # 领域事件总线（子系统订阅平台事件）
    "scheduler",     # 定时任务注册（任务体在子系统内）
    "ws",            # 仅 platform.py 可用（实时广播）
    "models",        # 仅 platform.py 可用，见下面单独一条
    "routers",       # 仅 platform.py 可用（居民端令牌依赖）
    "spd",           # 子系统内部
}

#: `spd/models.py` 唯一允许从平台 models 直取的名字：两个列类型。
#: 放宽到别的名字就等于把适配层旁路掉了。
COLUMN_TYPES = {"Money", "utcnow"}

#: 平台侧允许触碰子系统的两处，都是"装卸"性质，不是业务耦合。
PLATFORM_TOUCHPOINTS = {
    "models.py": "末尾 import 子系统模型，让 Base.metadata 认识 spd_* 表",
    "main.py": "调 register_spd / seed_spd 装卸子系统",
}


def _py_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _imports(path: str) -> list[tuple[int, str]]:
    """文件里出现的 import（含函数内的延迟 import），返回 (相对层级, 模块名)。

    层级必须一并返回：`from ..models import` 在 `app/spd/service.py` 里指的是
    **平台**的 models，在 `app/spd/routers/care.py` 里指的是**子系统自己**的
    models。只看名字会把这两件事混为一谈，而它们恰好是这套边界要区分的东西。
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((0, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.append((node.level or 0, node.module or ""))
    return out


def _imported_names(path: str, level: int, module: str) -> set[str]:
    """某条 `from ... import a, b` 引入的名字集合。"""
    tree = ast.parse(open(path, encoding="utf-8").read())
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and (node.level or 0) == level
            and (node.module or "") == module
        ):
            names.update(alias.name for alias in node.names)
    return names


def _escape_level(path: str) -> int:
    """从该文件出发，相对 import 到几层才离开子系统、落到平台的 `app` 包。

    `app/spd/x.py`（包深 1）→ 2 层；`app/spd/routers/x.py`（包深 2）→ 3 层。
    """
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(SPD_DIR))
    return len(os.path.dirname(rel).split(os.sep)) + 2 if os.path.dirname(rel) else 2


def test_平台不得依赖慢专病子系统():
    """依赖方向必须单向。平台侧只有两处触点，都在 PLATFORM_TOUCHPOINTS 里。"""
    offenders = []
    for path in _py_files(APP_DIR):
        if os.path.abspath(path).startswith(os.path.abspath(SPD_DIR)):
            continue
        name = os.path.basename(path)
        for _level, target in _imports(path):
            if "spd" not in target.split("."):
                continue
            if name in PLATFORM_TOUCHPOINTS:
                continue
            offenders.append(f"{os.path.relpath(path, APP_DIR)} → {target}")
    assert offenders == [], (
        "平台模块反过来依赖了慢专病子系统：\n  " + "\n  ".join(offenders)
        + "\n子系统可以依赖平台，平台不能依赖子系统。确需交互请走 app/events.py 的领域事件。"
    )


def test_子系统只依赖白名单内的平台能力():
    offenders = []
    for path in _py_files(SPD_DIR):
        rel = os.path.relpath(path, SPD_DIR)
        escape = _escape_level(path)
        for level, target in _imports(path):
            if level < escape:
                continue  # 没出子系统，属内部结构
            module = target.split(".")[0]
            if not module or module in PLATFORM_ALLOWLIST:
                continue
            offenders.append(f"spd/{rel} → {'.' * level}{target}")
    assert offenders == [], (
        "子系统依赖了白名单之外的平台模块：\n  " + "\n  ".join(offenders)
        + "\n请先在 tests/test_spd_boundary.py::PLATFORM_ALLOWLIST 与 "
          "app/spd/platform.py 里登记并说明理由。"
    )


def test_平台模型只能经适配层访问():
    """`app.models` 与 `app.routers` 只允许 `platform.py` 碰。

    收口的意义在实施期才显出来：平台调整 `Encounter` 的诊断字段时，
    改一处，而不是去九个路由文件里找。
    """
    offenders = []
    for path in _py_files(SPD_DIR):
        rel = os.path.relpath(path, SPD_DIR)
        if rel == "platform.py":
            continue
        escape = _escape_level(path)
        for level, target in _imports(path):
            if level < escape:
                continue
            if target == "models":
                # 唯一例外：spd/models.py 取两个列类型，用于断开导入环（理由见该文件）
                if rel == "models.py" and _imported_names(path, level, target) <= COLUMN_TYPES:
                    continue
                offenders.append(f"spd/{rel} → {'.' * level}models（平台模型）")
            if target.split(".")[0] == "routers":
                offenders.append(f"spd/{rel} → {'.' * level}{target}（平台路由）")
    assert offenders == [], (
        "子系统绕过适配层直接访问平台：\n  " + "\n  ".join(offenders)
        + "\n请改为 from ..platform import ...（app/spd/platform.py 是唯一入口）。"
    )


def test_子系统的表一律带前缀():
    from app.database import Base
    from app.spd import models as spd_models

    declared = {
        cls.__tablename__
        for cls in vars(spd_models).values()
        if hasattr(cls, "__tablename__")
    }
    bad = sorted(t for t in declared if not t.startswith(TABLE_PREFIX))
    assert bad == [], (
        f"子系统这些表没有 {TABLE_PREFIX} 前缀：{bad}；"
        "运维按前缀做备份、迁移与库内授权，前缀不是命名风格问题。"
    )
    # 反向：平台不该有 spd_ 前缀的表混进来
    平台表 = {
        name for name in Base.metadata.tables
        if name.startswith(TABLE_PREFIX)
    } - declared
    assert 平台表 == set(), f"这些 {TABLE_PREFIX} 表不在子系统模型里：{sorted(平台表)}"


def test_平台表不得外键指向子系统表():
    """数据层的依赖方向也要单向——平台表一旦引用 spd_*，子系统就卸不掉了。"""
    from app.database import Base

    offenders = []
    for table in Base.metadata.tables.values():
        if table.name.startswith(TABLE_PREFIX):
            continue
        for fk in table.foreign_keys:
            if fk.column.table.name.startswith(TABLE_PREFIX):
                offenders.append(f"{table.name} → {fk.target_fullname}")
    assert offenders == [], (
        "平台表外键指向了子系统表：\n  " + "\n  ".join(offenders)
        + "\n子系统可以引用平台主数据，反过来不行。"
    )


def test_子系统路由只长在自己的前缀下():
    from app.main import app

    strays = sorted({
        route.path
        for route in app.routes
        if getattr(route, "endpoint", None) is not None
        and getattr(route.endpoint, "__module__", "").startswith("app.spd")
        and not route.path.startswith(API_PREFIXES)
    })
    assert strays == [], f"这些子系统接口没长在 {API_PREFIXES} 下：{strays}"

    # 反向：这些前缀下的接口必须来自子系统，不许平台在同一前缀下塞东西
    intruders = sorted({
        route.path
        for route in app.routes
        if getattr(route, "endpoint", None) is not None
        and route.path.startswith(API_PREFIXES)
        and not getattr(route.endpoint, "__module__", "").startswith("app.spd")
    })
    assert intruders == [], f"这些接口占用了子系统前缀却不属于子系统：{intruders}"


def test_关闭开关后不注册任何路由(monkeypatch):
    """`MEDPLAT_SPD_ENABLED=false` 的县不该在菜单里看到用不上的功能。"""
    from app.config import settings

    monkeypatch.setattr(settings, "spd_enabled", False)
    blank = FastAPI()
    assert register_spd(blank) == 0
    assert not [r for r in blank.routes if getattr(r, "path", "").startswith("/api/spd")]

    monkeypatch.setattr(settings, "spd_enabled", True)
    enabled = FastAPI()
    assert register_spd(enabled) == 9, "九个路由模块应一次装上"


def test_种子在关闭时不落数据(monkeypatch):
    from app.config import settings
    from app.spd import seed_spd

    monkeypatch.setattr(settings, "spd_enabled", False)
    assert seed_spd(None) is False, "关闭时不该碰数据库（传 None 也不该炸）"


@pytest.mark.parametrize("module", ["models", "rules", "service", "seed", "platform"])
def test_子系统包结构齐全(module):
    """包内五个模块各司其职：模型、规则、领域服务、种子、平台适配。"""
    assert os.path.exists(os.path.join(SPD_DIR, f"{module}.py"))


# ============================================================ 迁移与运维隔离


def _migration_files():
    versions = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions")
    for name in sorted(os.listdir(versions)):
        if name.endswith(".py"):
            yield name, open(os.path.join(versions, name), encoding="utf-8").read()


def _revisions():
    """(revision, down_revision, branch_labels, 文件名) 四元组。"""
    import re

    out = []
    for name, text in _migration_files():
        rev = re.search(r"^revision(?::\s*str)?\s*=\s*['\"]([^'\"]+)", text, re.M)
        down = re.search(r"^down_revision(?::[^=]+)?\s*=\s*['\"]([^'\"]+)['\"]", text, re.M)
        labels = re.search(r"^branch_labels\s*=\s*\(([^)]*)\)", text, re.M)
        if rev:
            out.append((
                rev.group(1),
                down.group(1) if down else None,
                (labels.group(1).replace('"', "").replace("'", "").strip(" ,") if labels else ""),
                name,
            ))
    return out


def test_子系统迁移自成一条分支():
    """慢专病与主平台并行开发时不该抢同一个 alembic head。

    代价是升级命令要用 `alembic upgrade heads`（复数），已在运维手册里统一。
    """
    labeled = [r for r in _revisions() if r[2] == "spd"]
    assert labeled, "慢专病迁移应带 branch_labels = (\"spd\",)"
    assert len(labeled) == 1, f"分支标签只该打在分支起点上：{[r[3] for r in labeled]}"


def test_平台迁移不得挂到子系统分支上():
    """平台的新迁移接到 spd 链上，就等于把子系统焊死在平台的升级顺序里。"""
    revisions = _revisions()
    spd_revs = {r[0] for r in revisions if r[2] == "spd" or "spd" in r[3].lower()}
    offenders = [
        f"{r[3]}（down_revision={r[1]}）"
        for r in revisions
        if r[1] in spd_revs and r[0] not in spd_revs and "spd" not in r[3].lower()
    ]
    assert offenders == [], (
        "以下平台迁移挂到了慢专病分支上：\n  " + "\n  ".join(offenders)
        + "\n平台迁移请挂回平台分支的 head。"
    )


def test_分子系统备份脚本只导子系统的表():
    """脚本存在且确实按前缀过滤——不然"分子系统备份"只是个文件名。"""
    script = os.path.join(os.path.dirname(__file__), "..", "scripts", "spd_backup.sh")
    assert os.path.exists(script)
    text = open(script, encoding="utf-8").read()
    assert 'PREFIX="spd_"' in text
    assert "恢复目标库必须已有对应的 users/organizations/patients" in text, (
        "必须写明这份导出不自洽——以为备份了其实恢复不了，是备份最常见的坑"
    )
