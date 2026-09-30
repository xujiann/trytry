"""运维导入脚本不在生产环境、不在 dry-run 时建表（P2-1089，第三十一批「运维脚本与后台任务」扫描 C2-7；ADR-0002 延伸到 scripts/）。

四个导入脚本（存量、账号、收费目录、字典）原先无条件 `Base.metadata.create_all`，dry-run 也照做——DDL 在 dry-run 的事务之外先
执行、自动提交。实测（修前）：空库表数 0 → 对它 dry-run 一次导入预检，报「将导入 1 条」→ 之后表数 261 → `alembic upgrade
heads` exit 1「table code_systems already exists」：发版窗口里用新代码对还没迁移的库做一次预检，发布流程的迁移就失败、实例
起不来。生产库的结构只走迁移（`main.lifespan` 早有同一个守卫）；dry-run 说的是「不落库」，建表也算落库。
"""
import sys
from pathlib import Path

import pytest

from app.config import settings
from app.database import Base, create_all_for_scripts

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture()
def create_all_spy(monkeypatch):
    calls = []
    monkeypatch.setattr(Base.metadata, "create_all", lambda *a, **k: calls.append(1))
    return calls


def test_dry_run不建表_生产不建表_开发照旧建(monkeypatch, create_all_spy):
    create_all_for_scripts(dry_run=True)
    assert create_all_spy == []
    monkeypatch.setattr(settings, "env", "prod")
    create_all_for_scripts(dry_run=False)
    assert create_all_spy == []
    monkeypatch.setattr(settings, "env", "dev")
    create_all_for_scripts(dry_run=False)
    assert create_all_spy == [1]   # 开发环境「空库直跑」的方便照旧


def test_收费目录导入的dry_run不建表(client, tmp_path, create_all_spy):   # client：先让应用把库表建好
    from import_charge_items import run_import

    csv = tmp_path / "dry.csv"
    csv.write_text("code,name,category,price,active\nP21089,校验项目,other,9.90,true\n", encoding="utf-8")
    run_import(csv, dry_run=True)
    assert create_all_spy == []   # 修前：dry-run 也 create_all


def test_脚本里不再直接调create_all():
    offenders = [p.name for p in SCRIPTS.glob("*.py") if "Base.metadata.create_all(" in p.read_text(encoding="utf-8")]
    assert offenders == [], offenders   # 一律走 create_all_for_scripts
