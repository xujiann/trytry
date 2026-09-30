"""生产形态起在没迁移到当前版本的库上：种子块逐个报 ERROR 跳过，`/api/health` 仍 200 ok，业务请求全 500（P2-1141，
第三十三批扫描 A1-3）。

生产不 `create_all`（ADR-0002），结构全靠起服务前的 `alembic upgrade heads`。漏跑时——多实例设了
`MEDPLAT_MIGRATE_ON_START=0` 却忘了发布流程第 2 步、裸机没配 `ExecStartPre`、升级漏跑一版——实例照样起来，health 只探
`SELECT 1`。修前实测（scan33 a1/r6，空库、不建表）：16 条「种子块…失败，跳过」ERROR，`health 200 {'status': 'ok',
'database': 'ok'}`，`login 500`——探针是绿的，负载均衡照常放流量。

修法：生产那一支启动时比对库里的 `alembic_version` 与迁移脚本的两个 head（平台链 + spd 链），缺了就在 ERROR 日志里
点名缺哪个 head，health 回 503、`status` 记 degraded，另多写一个 `reason`「数据库未迁移到当前版本」（原有四个键不变，
没有原因时不出这个键）。不拒启、迁移机制不动。只判「库落后」：只回代码不回库（发布流程的首选回滚）时库里是本版不认识的
版本，不算缺。开发 / 测试（create_all 那一支）不比对。
"""
import logging
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import reset_database

from app.config import settings
from app.database import engine
from app.main import app

SCRIPT = ScriptDirectory.from_config(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")))
SPD_HEAD = next(h for h in SCRIPT.get_heads() if "spd" in SCRIPT.get_revision(h).branch_labels)
PLATFORM_HEAD = next(h for h in SCRIPT.get_heads() if h != SPD_HEAD)
REASON = "数据库未迁移到当前版本：先 alembic upgrade heads，再重启本实例（缺哪个迁移 head 见启动日志）"
HEALTHY = {"status": "ok", "service": "medplat", "version": app.version, "database": "ok"}


def _stamp(*versions):
    """把库的迁移版本表摆成给定的几行；一行不给就是没有这张表（从没跑过迁移）。"""
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        if versions:
            conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
            for version in versions:
                conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": version})


@pytest.fixture()
def prod(monkeypatch):
    """生产形态：lifespan 走不 create_all 的那一支。库表由 reset_database 备好（种子照常种），只摆迁移版本表。"""
    reset_database()
    monkeypatch.setattr(settings, "env", "prod")
    # 生产校验拒绝默认口令 / 密钥（同 test_adr0002_create_all_guard），换成非默认值让应用能启动
    monkeypatch.setattr(settings, "secret", "p2-1141-test-secret-not-default")
    monkeypatch.setattr(settings, "admin_password", "p2-1141-test-password")
    yield
    _stamp()   # 迁移版本表不在 Base.metadata 里，reset_database 删不掉它，用完自己收


def _health():
    with TestClient(app) as client:
        resp = client.get("/api/health")
    return resp.status_code, resp.json()


def _missing_log(caplog) -> str:
    (record,) = [r for r in caplog.records if "数据库未迁移到当前版本" in r.getMessage()]
    assert record.levelno == logging.ERROR
    return record.getMessage()


def test_没跑过迁移的库以生产形态起_health503且写明原因(prod, caplog):
    _stamp()
    with caplog.at_level(logging.ERROR, logger="medplat.seed"):
        code, body = _health()
    assert code == 503, body   # 修前 200 {'status': 'ok', …, 'database': 'ok'}
    assert body == {**HEALTHY, "status": "degraded", "reason": REASON}
    message = _missing_log(caplog)
    assert f"{PLATFORM_HEAD}（平台链）" in message and f"{SPD_HEAD}（spd 链）" in message


def test_平台链落后一版_日志只点名缺的那个head(prod, caplog):
    parent = SCRIPT.get_revision(PLATFORM_HEAD).down_revision
    assert isinstance(parent, str)   # 用例前提：平台链的 head 只有一个父版本
    _stamp(parent, SPD_HEAD)
    with caplog.at_level(logging.ERROR, logger="medplat.seed"):
        code, body = _health()
    assert (code, body) == (503, {**HEALTHY, "status": "degraded", "reason": REASON})
    assert f"缺迁移 head {PLATFORM_HEAD}（平台链）（库里是" in _missing_log(caplog)


def test_迁移齐了_200_响应不多键(prod, caplog):
    _stamp(PLATFORM_HEAD, SPD_HEAD)
    with caplog.at_level(logging.ERROR, logger="medplat.seed"):
        assert _health() == (200, HEALTHY)
    assert not [r for r in caplog.records if "数据库未迁移" in r.getMessage()]


def test_库比代码新_只回代码不回库不算缺(prod):
    """发布流程的首选回滚是只回镜像、保留新库：库里是本版不认识的版本（新版加的迁移），本版的 head 是它的祖先。"""
    _stamp("ffffffffffff", SPD_HEAD)
    assert _health() == (200, HEALTHY)


def test_开发形态不比对_也不带上一次启动的结论(prod, monkeypatch):
    _stamp()
    assert _health()[0] == 503   # 生产形态、没迁移
    monkeypatch.setattr(settings, "env", "dev")
    assert _health() == (200, HEALTHY)   # create_all 那一支：开发 / 测试库本来就没有迁移版本表，不比对
