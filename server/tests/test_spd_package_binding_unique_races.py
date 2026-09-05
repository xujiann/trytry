"""真 PostgreSQL 上的服务包"在绑唯一"竞态（P1-30，默认跳过）。

为什么必须上真 PG：SQLite 的**库级写锁**把"预检 → 插入"之间的窗口一并锁掉了，
本地线程探针即使把索引拆掉也大概率不重复——它测不出这个洞。PG 是
READ COMMITTED，两路并发各自查空、各自 INSERT，旧代码下 8 路会得到
**8 个 201**：同一份档案八条 bound，`items[].used` 的配额凭空翻八倍，
居民端与工作台按 `status='bound'` 逐条渲染，出现八张同名服务包卡片。

修好之后这里应当看到 1×201 + 7×409：预检晚于赢家提交的那几路在预检就被挡下，
预检早于赢家提交的那几路走到 INSERT，被 PG 挂在赢家未提交的索引项上等待，
赢家一提交就抬 IntegrityError，`_bind_package` 回滚并翻成与预检**同一句** 409。
调用方分不出"本来就重复"与"并发撞车"——这正是要的。

开启方式（与 tests/test_postgres_real.py 同一约定）：

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_spd_package_binding_unique_races.py -q

本档**不重建 schema**（不 DROP SCHEMA）：该库可能被其他用例共用，所有数据
都用带随机后缀的名字自建，跑完各走各的。
"""
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"
    ),
]

SERVER_DIR = Path(__file__).resolve().parents[1]
INDEX_NAME = "uq_spd_pkg_binding_enroll_pkg_bound"
DUP_DETAIL = "该服务包已绑定"


def _retrying(fn, what: str, attempts: int = 5, wait: int = 60):
    """共用库上撞锁就等一会儿再来——跳过测试等于把红灯藏起来。"""
    from sqlalchemy.exc import DBAPIError, OperationalError

    for i in range(attempts):
        try:
            return fn()
        except (OperationalError, DBAPIError) as exc:  # 锁等待/串行化冲突
            if i == attempts - 1:
                raise
            print(f"[{what}] 第 {i + 1} 次撞锁（{type(exc).__name__}），{wait}s 后重试")
            time.sleep(wait)
    raise AssertionError("unreachable")


@pytest.fixture(scope="module")
def pg_engine():
    """连上共用的 PG 测试库；表还没有就跑一次迁移（幂等，不 DROP）。"""
    from sqlalchemy import create_engine, inspect

    engine = create_engine(PG_URL)
    if "spd_package_bindings" not in inspect(engine).get_table_names():
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR,
            env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"迁移在 PG 上失败：\n{result.stderr[-2000:]}"
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def world(pg_engine):
    """机构 + 患者 + 服务包，名字全带随机后缀（共用库，不与别人抢唯一键）。"""
    from sqlalchemy.orm import sessionmaker

    from app.models import Organization, Patient
    from app.spd.models import SpdServicePackage

    tag = uuid.uuid4().hex[:8]
    Session = sessionmaker(bind=pg_engine)

    def build():
        with Session() as db:
            org = Organization(name=f"服务包竞态院-{tag}", org_type="township",
                               level="township")
            patient = Patient(name=f"服务包竞态-{tag}", id_card=f"3309{tag}0011",
                              gender="男", birth_date="1960-01-01",
                              ehc_no=f"PKG-EHC-{tag}")  # 直连建档要自带健康卡号
            package = SpdServicePackage(
                code=f"pkgrace_{tag}", name=f"竞态服务包-{tag}",
                program_code=f"pkgrace_dm_{tag}", price=200, period_days=30,
                items=[{"code": "bp_check", "name": "血压测量", "times": 2, "price": 5}],
            )
            db.add_all([org, patient, package])
            db.commit()
            return {"Session": Session, "org_id": org.id, "patient_id": patient.id,
                    "package_id": package.id, "program_code": package.program_code,
                    "tag": tag, "seq": 0}

    return _retrying(build, "建场景")


def _new_enrollment(world) -> int:
    """每条用例一份新档案：竞态跑完会留下 bound 行，共用档案会互相污染。"""
    from app.spd.models import SpdEnrollment

    world["seq"] += 1

    def build():
        with world["Session"]() as db:
            enrollment = SpdEnrollment(
                patient_id=world["patient_id"],
                program_code=f'{world["program_code"]}_{world["seq"]}',
                org_id=world["org_id"], status="active",
            )
            db.add(enrollment)
            db.commit()
            return enrollment.id

    return _retrying(build, "建档案")


def _bind_once(world, enrollment_id: int) -> int:
    """`bind_package` 的函数体等价物：预检 → `_bind_package` → commit。

    刻意不走 TestClient：要测的是"预检与插入之间没有闸门"这段，
    HTTP 层与鉴权在这里只会稀释信号。
    """
    from fastapi import HTTPException

    from app.spd.models import SpdEnrollment, SpdPackageBinding
    from app.spd.routers.population import _bind_package

    with world["Session"]() as db:
        enrollment = db.get(SpdEnrollment, enrollment_id)
        exists = (
            db.query(SpdPackageBinding)
            .filter(
                SpdPackageBinding.enrollment_id == enrollment_id,
                SpdPackageBinding.package_id == world["package_id"],
                SpdPackageBinding.status == "bound",
            )
            .first()
        )
        if exists is not None:
            return 409
        try:
            _bind_package(db, enrollment, world["package_id"])
            db.commit()
        except HTTPException as exc:
            db.rollback()
            assert exc.detail == DUP_DETAIL, f"兜底文案漂了：{exc.detail!r}"
            return exc.status_code
        return 201


def _race(world, enrollment_id: int, times: int = 8) -> list[int]:
    """Barrier 卡住所有线程再一起放行：不加栅栏窗口根本打不开。"""
    codes: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(times)

    def run():
        barrier.wait(timeout=60)
        code = _bind_once(world, enrollment_id)
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=run) for _ in range(times)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sorted(codes)


def _rows(world, enrollment_id: int) -> list[str]:
    from app.spd.models import SpdPackageBinding

    with world["Session"]() as db:
        return [
            b.status
            for b in db.query(SpdPackageBinding)
            .filter(SpdPackageBinding.enrollment_id == enrollment_id,
                    SpdPackageBinding.package_id == world["package_id"])
            .order_by(SpdPackageBinding.id)
            .all()
        ]


def test_八路并发绑定同一服务包只成一条(world):
    """旧代码在 PG 上是 8×201 + 8 条 bound（配额翻八倍）；修好后 1×201 + 7×409。"""
    eid = _new_enrollment(world)
    codes = _race(world, eid)

    assert codes.count(201) == 1, f"赢家不止一个：{codes}"
    assert codes.count(409) == 7, f"输家没有全部拿到 409：{codes}"
    assert codes == [201] + [409] * 7
    # 输家全部回滚，库里不留残渣：总行数 1，在绑 1
    assert _rows(world, eid) == ["bound"]


def test_解绑后再并发绑定仍然只成一条(world):
    """解绑后重绑是合法续期路径（部分索引只锁 bound），但它同样是 check-then-act。"""
    from app.spd.models import SpdPackageBinding

    eid = _new_enrollment(world)
    assert _race(world, eid) == [201] + [409] * 7

    def unbind():
        with world["Session"]() as db:
            row = (
                db.query(SpdPackageBinding)
                .filter(SpdPackageBinding.enrollment_id == eid,
                        SpdPackageBinding.status == "bound")
                .one()
            )
            row.status = "unbound"
            db.commit()

    _retrying(unbind, "解绑")

    assert _race(world, eid) == [201] + [409] * 7, "解绑后重绑这一轮也必须只成一条"
    # 台账保留：一条历史 unbound + 一条新的 bound
    assert _rows(world, eid) == ["unbound", "bound"]


def test_部分索引只锁在绑不碰历史台账(world):
    """全量唯一会把"解绑保留台账"这条既有行为反噬掉——按真 PG 语义钉一遍。"""
    import sqlalchemy.exc

    from app.spd.models import SpdPackageBinding

    eid = _new_enrollment(world)

    def binding(status):
        return SpdPackageBinding(enrollment_id=eid, package_id=world["package_id"],
                                 items=[], status=status, period_end="")

    def probe():
        with world["Session"]() as db:
            db.add(binding("unbound"))
            db.add(binding("unbound"))  # 两条历史台账必须能共存
            db.add(binding("bound"))
            db.commit()

            db.add(binding("bound"))  # 第二条在绑必须撞索引
            with pytest.raises(sqlalchemy.exc.IntegrityError):
                db.commit()
            db.rollback()

    _retrying(probe, "部分索引探针")


def test_在绑唯一索引真的建在PG上(pg_engine):
    """模型声明了、PG 上没建（漏迁移或迁移里探到冲突跳过了）同样等于没有约束。"""
    from sqlalchemy import inspect

    indexes = {i["name"]: i for i in inspect(pg_engine).get_indexes("spd_package_bindings")}
    assert INDEX_NAME in indexes, (
        f"PG 上没有 {INDEX_NAME}：迁移没跑，或探到存量冲突后跳过了建索引"
        "（此时应按迁移 docstring 的处置 SQL 先清冲突再补建）"
    )
    index = indexes[INDEX_NAME]
    assert index["unique"], f"{INDEX_NAME} 不是唯一索引，等于没有约束"
    assert index["column_names"] == ["enrollment_id", "package_id"], "索引的键变了"
    where = str(index.get("dialect_options", {}).get("postgresql_where", ""))
    assert "bound" in where, f"{INDEX_NAME} 不再是部分索引：{where!r}"
