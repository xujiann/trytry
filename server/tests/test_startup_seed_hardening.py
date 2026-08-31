"""启动种子化加固（TECH_DEBT P1-9）：逐块隔离 + 多实例竞态免崩。

账本登记的两条风险，各钉一根柱子：

1. **一条脏种子 = 全站不可用**：十几个种子块此前裸跑，任何一块抛错整个启动
   打死。现在逐块过 `_seed_step`——参考数据缺一块是降级（记 ERROR，运维按
   日志补），起不来才是事故。
2. **多实例空库首启的查-插竞态**：种子全是"查已有 code 再 add"，两个实例
   同时对空库启动都看到"没有"、都插，慢的撞 unique 崩掉启动。两层防：
   PG 上 advisory lock 把种子阶段跨实例串行化（专用连接持锁——种子有十几次
   commit，事务锁第一次 commit 就没、Session 会话锁 commit 归还连接池后就
   不在同一条连接上，这两条歪路都走不通）；撞上唯一键时按"另一实例已先
   种好"处理，rollback 后继续，不算失败。

变异验证（写入时逐一做过）：去掉 _seed_step 的 IntegrityError 分流 →
撞键用例红；去掉整个 try → 脏种子用例连启动都起不来；删 pg_advisory_lock
接线 → 接线钉红。
"""
import inspect
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from conftest import reset_database

import app.main as main_mod
from app.database import SessionLocal
from app.main import _seed_step, app


# ================================================================ _seed_step 本体


def test_撞唯一键按已种好处理不抛出(caplog):
    """竞态窗口的观测面：陈旧快照 → 重复插入 → commit 撞 unique → 吞掉继续。"""
    reset_database()
    from app.models import CodeSystem

    db = SessionLocal()
    try:
        db.add(CodeSystem(code="race_probe", name="竞态探针"))
        db.commit()

        def stale_snapshot_insert():
            # 模拟"另一实例在本实例查完之后、插入之前先种好了"——直接重复插
            db.add(CodeSystem(code="race_probe", name="竞态探针"))
            db.commit()

        with caplog.at_level(logging.INFO, logger="medplat.seed"):
            _seed_step(db, "竞态探针块", stale_snapshot_insert)  # 不得抛出
        assert any("撞唯一键" in r.message for r in caplog.records)
        # 会话必须已被 rollback 救回来，后续块还要用它
        assert db.query(CodeSystem).filter_by(code="race_probe").count() == 1
    finally:
        db.close()


def test_普通异常记错误日志后继续(caplog):
    db = SessionLocal()
    try:
        def broken():
            raise ValueError("脏种子")

        with caplog.at_level(logging.ERROR, logger="medplat.seed"):
            _seed_step(db, "坏块", broken)  # 不得抛出
        assert any("坏块" in r.message and "失败" in r.message for r in caplog.records)
    finally:
        db.close()


def test_IntegrityError分流不误吞其他异常():
    """IntegrityError 走 INFO、其他异常走 ERROR——分流本身也要可证。"""
    db = SessionLocal()
    try:
        ran = []

        def ok():
            ran.append(1)

        _seed_step(db, "好块", ok)
        assert ran == [1]

        def integrity():
            raise IntegrityError("stmt", {}, Exception("dup"))

        _seed_step(db, "撞键块", integrity)  # 两类都不得外抛
    finally:
        db.close()


# ================================================================ 启动级行为


def test_单块脏种子不拖垮启动_后续块照种(monkeypatch, caplog):
    """P1-9 主诉求：一条脏种子此前=全站不可用，现在=缺那一块地降级启动。"""
    reset_database()
    # 毒化"法定传染病目录"块的种子源（运行时才 import，monkeypatch 模块属性即生效）
    import app.routers.infectious as infectious_mod

    monkeypatch.setattr(infectious_mod, "SEED_DISEASES", [{"不存在的列": 1}])
    with caplog.at_level(logging.ERROR, logger="medplat.seed"):
        with TestClient(app) as client:  # 启动必须成功——这就是本用例的核心断言
            resp = client.post(
                "/api/auth/login", json={"username": "admin", "password": "admin123"}
            )
            assert resp.status_code == 200, "脏种子不该影响管理员账号块"
    assert any("法定传染病目录" in r.message for r in caplog.records), "失败块必须点名进日志"
    from app.models import DrgGroup, InfectiousDisease

    db = SessionLocal()
    try:
        assert db.query(InfectiousDisease).count() == 0, "毒化块确实没种进去（用例前提自证）"
        assert db.query(DrgGroup).count() > 0, "排在坏块之后的 DRG 目录必须照常种上"
    finally:
        db.close()


def test_干净启动后种子齐全_与加固前行为一致():
    reset_database()
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login", json={"username": "admin", "password": "admin123"}
        ).status_code == 200
    from app.models import ChronicDiseaseType, CodeSystem, DrgGroup, InfectiousDisease

    db = SessionLocal()
    try:
        for model in (CodeSystem, InfectiousDisease, ChronicDiseaseType, DrgGroup):
            assert db.query(model).count() > 0, model.__name__
    finally:
        db.close()


# ================================================================ PG 串行化接线钉


def test_pg种子锁接线_专用连接与finally解锁():
    """无真 PG 环境的静态钉（与审计链 test_pg_advisory_lock_sql_renders 同法）：

    - 锁必须拿在 `engine.connect()` 的**专用连接**上（种子有十几次 commit，
      事务级锁第一次 commit 就没了、Session 会话锁 commit 归还连接池后就不在
      同一条连接上——这两条歪路都不接受）；
    - 按 postgresql 方言分流；finally 里 unlock + close。
    """
    src = inspect.getsource(main_mod.lifespan)
    assert "pg_advisory_lock" in src, "种子阶段的跨实例串行化锁被拆了——P1-9 竞态回归"
    assert "pg_advisory_unlock" in src, "只锁不解，第二实例要等到连接死才放行"
    assert "engine.connect()" in src, "锁必须挂在专用连接上，不能挂在种子用的 Session 上"
    assert '"postgresql"' in src, "必须按方言分流（SQLite 单写者无需锁）"
    assert isinstance(main_mod._SEED_PG_LOCK_KEY, int)
    assert main_mod._SEED_PG_LOCK_KEY != main_mod._AUDIT_PG_LOCK_KEY, "与审计链锁键不得撞号"


def test_全部种子块都走隔离通道():
    """新加种子块绕开 _seed_step 裸跑，一步抛错又会打死启动——数量钉住。"""
    src = inspect.getsource(main_mod.lifespan)
    assert "for step_name, step_fn in (" in src
    # 14 个具名块进循环 + rbac 两步（内置角色/权限点登记）单独走 _seed_step
    assert src.count("def _seed_") == 14, "种子块数量变了？新块要么进循环要么说明为何例外"
    assert src.count("_seed_step(db,") == 3, "循环 + rbac 两步，共三处调用点"
