"""医保两类待遇申报的真并发取证（P1-30，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_insurance_apply_unique_races.py -q

为什么非真 PG 不可：`apply_special_disease` / `apply_dual_channel` 现在只有一条
写入路径（`insert_or_conflict`），抢输者要在**赢家 commit 之后**才在自己的
INSERT 上收到 unique_violation——SQLite 的库级写锁把判定与写入之间的窗口一并锁
掉了，八路并发在那里根本排不出这个先后。同一份代码在 SQLite 上永远绿，正是
P1-29 那三条不变式的教训（见 tests/test_logical_unique_races.py 的模块注释）。

本档的不变量：**恰一路成功落库，其余七路拿到与顺序重复完全相同的 409，库里
恰一行**；且 `IntegrityError` 一次都不许漏给调用方（漏出去就是 500 + 丢单）。
把索引拆掉或把 `insert_or_conflict` 换回 `db.add/commit`，第一阶段会变成
8 路全 ok、库里 8 行、errors 为空——静默双写，正是要防的那个形状。

**这套库是多人共用的**：本档只用带随机后缀的自建数据，跑完自己收拾干净，
并且**从不 DROP SCHEMA**——清了别人正在跑的用例就没了。空库则就地补跑迁移
（只做加法），见 `pg_engine` 的注释。
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

from test_postgres_real import _race_on_pg  # Barrier 真并发的既有夹具，不另造一份

from app.models import DualChannelApp, Patient, SpecialDiseaseApp, User
from app.routers.insurance import (
    DualChannelCreate,
    SpecialDiseaseCreate,
    apply_dual_channel,
    apply_special_disease,
)

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"
    ),
]

# 与 tests/test_insurance_apply_unique.py（顺序档）同一份文案：两处读起来不一样
# 就说明接口层分叉出了第二条路径，那正是本轮要消灭的东西。
SPECIAL_409 = "该患者同病种已有待审核的特病申报，不可重复申报"
DUAL_409 = "该患者该药品已有待审核的双通道申报，请先由管理层审核后再申报"

RACERS = 8

SERVER_DIR = Path(__file__).resolve().parents[1]

# 本档赖以成立的两条部分唯一索引：表名 → 索引名。
GUARDED_INDEXES = (
    ("special_disease_apps", "uq_special_disease_app_applied"),
    ("dual_channel_apps", "uq_dual_channel_pending"),
)


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，**任何情况下都不清 schema**。

    为什么必须自带建表：本档按文件名排在 `test_postgres_real.py` 与
    `test_schema_governance.py` 之前（i < p < s），而全仓库只有那两处会
    `DROP SCHEMA public CASCADE` + `alembic upgrade heads` 把表建出来。CI 的
    integration 档（`pytest tests/ -q -m integration`，阻断）每次配一个全新的
    postgres service，本档第一个跑到、库里一张表都没有——靠别的文件先建好表
    是隐式的跨文件顺序依赖，实测就是整档 4 个 ERROR（`NoSuchTableError:
    special_disease_apps`），闸门直接变红且本档一条都没真跑。故这里自己升到
    heads：**只做加法**，共用的开发库上表已存在，这一步是空操作。

    表在了就只确认两条部分唯一索引真在库上，**不补跑迁移**——"表在、索引没了"
    正是索引被人拆掉的形状，那种情况本档测到的不是"索引拦住了"而是"八路碰巧
    没撞上"，必须当场报红，不能被一次静默的 upgrade 补回去。
    """
    engine = create_engine(PG_URL)
    inspector = sa_inspect(engine)
    if not all(inspector.has_table(table) for table, _ in GUARDED_INDEXES):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR,
            env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"迁移在 PG 上失败：\n{result.stderr[-2000:]}"
        inspector = sa_inspect(engine)  # 反射有缓存，建完表要换一把新的
    for table, index_name in GUARDED_INDEXES:
        names = {i["name"] for i in inspector.get_indexes(table)}
        assert index_name in names, (
            f"{table} 上没有 {index_name}：请先在该库跑 alembic upgrade heads"
        )
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def actors(pg_engine):
    """一名患者 + 一名申报人，名字带随机后缀，与同库上别的用例互不打扰。"""
    Session = sessionmaker(bind=pg_engine)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        patient = Patient(
            name=f"PG申报并发患者{tag}",
            id_card=f"3309{uuid.uuid4().int % 10**14:014d}",
            gender="男", birth_date="1985-05-05",
            ehc_no=f"PG-INS-{tag}",  # 健康卡号由接口层发号，直连建档要自带
        )
        user = User(username=f"pg_ins_{tag}", password_hash="x", full_name=f"并发申报员{tag}")
        db.add_all([patient, user])
        db.commit()
        ids = {"patient_id": patient.id, "user_id": user.id, "tag": tag}

    yield ids

    # 共用库：自己造的行自己收拾（先子后父，否则 created_by 外键拦着）
    with Session() as db:
        db.query(DualChannelApp).filter_by(patient_id=ids["patient_id"]).delete()
        db.query(SpecialDiseaseApp).filter_by(patient_id=ids["patient_id"]).delete()
        db.commit()
        db.query(Patient).filter_by(id=ids["patient_id"]).delete()
        db.query(User).filter_by(id=ids["user_id"]).delete()
        db.commit()


def _split(results):
    """把 (标记, …) 元组分成成功组与 409 组。"""
    return (
        [r for r in results if r[0] == "ok"],
        [r for r in results if r[0] == "409"],
    )


# ================================================================ 特病申报


def test_并发特病申报_恰一条待批其余全是同一句409(pg_engine, actors):
    Session = sessionmaker(bind=pg_engine)
    pid, disease = actors["patient_id"], "尿毒症透析"

    def worker(i):
        with Session() as db:
            try:
                obj = apply_special_disease(
                    SpecialDiseaseCreate(
                        patient_id=pid, disease_name=disease, reason=f"并发{i}"
                    ),
                    db=db,
                )
            except HTTPException as exc:
                return ("409", exc.status_code, exc.detail)
            return ("ok", obj.id, obj.status)

    results, errors = _race_on_pg(worker, times=RACERS)
    assert not errors, f"约束冲突不该漏给调用方（那是 500 + 丢单）：{errors}"
    assert len(results) == RACERS
    oks, conflicts = _split(results)
    assert len(oks) == 1, f"恰一路该落库，实际 {results}"
    assert oks[0][2] == "applied"
    assert len(conflicts) == RACERS - 1
    assert {(c[1], c[2]) for c in conflicts} == {(409, SPECIAL_409)}, (
        f"抢输者的回执必须与顺序重复逐字节一致：{conflicts}"
    )

    with Session() as db:
        rows = db.query(SpecialDiseaseApp).filter_by(
            patient_id=pid, disease_name=disease
        ).all()
        assert len(rows) == 1 and rows[0].status == "applied", (
            f"同患者同病种应恰一条，实际 {[(r.id, r.status) for r in rows]}"
        )
        # 阶段二：驳回之后这个键重新可用，但仍然只放一条进去（部分索引的边界）
        rows[0].status = "rejected"
        db.commit()

    results, errors = _race_on_pg(worker, times=RACERS)
    assert not errors, errors
    oks, conflicts = _split(results)
    assert len(oks) == 1, f"驳回后重申仍应恰一路成功，实际 {results}"
    assert {(c[1], c[2]) for c in conflicts} == {(409, SPECIAL_409)}

    with Session() as db:
        rows = db.query(SpecialDiseaseApp).filter_by(
            patient_id=pid, disease_name=disease
        ).all()
        assert sorted(r.status for r in rows) == ["applied", "rejected"], (
            f"驳回那条是历史、新的一条待批，实际 {[(r.id, r.status) for r in rows]}"
        )


def test_并发特病申报_不同病种互不阻塞(pg_engine, actors):
    """索引只锁 (patient_id, disease_name) 这一对：八个病种就该八条全过。

    这条是"没有过度拦截"的取证——真拦多了，用例会以 409 的形式当场现形。
    """
    Session = sessionmaker(bind=pg_engine)
    pid = actors["patient_id"]

    def worker(i):
        with Session() as db:
            try:
                obj = apply_special_disease(
                    SpecialDiseaseCreate(
                        patient_id=pid, disease_name=f"并发病种-{i}", reason="互不阻塞"
                    ),
                    db=db,
                )
            except HTTPException as exc:
                return ("409", exc.status_code, exc.detail)
            return ("ok", obj.id, obj.status)

    results, errors = _race_on_pg(worker, times=RACERS)
    assert not errors, errors
    oks, conflicts = _split(results)
    assert conflicts == [], f"不同病种不该互相拦：{conflicts}"
    assert len(oks) == RACERS

    with Session() as db:
        rows = db.query(SpecialDiseaseApp).filter(
            SpecialDiseaseApp.patient_id == pid,
            SpecialDiseaseApp.disease_name.like("并发病种-%"),
        ).all()
        assert len(rows) == RACERS


# ================================================================ 双通道申报


def test_并发双通道申报_恰一条待审其余全是同一句409(pg_engine, actors):
    Session = sessionmaker(bind=pg_engine)
    pid, drug = actors["patient_id"], "阿达木单抗"

    def worker(i):
        with Session() as db:
            user = db.get(User, actors["user_id"])
            try:
                receipt = apply_dual_channel(
                    DualChannelCreate(patient_id=pid, drug_name=drug, reason=f"并发{i}"),
                    db=db,
                    user=user,
                )
            except HTTPException as exc:
                return ("409", exc.status_code, exc.detail)
            return ("ok", receipt["id"], receipt["status"])

    results, errors = _race_on_pg(worker, times=RACERS)
    assert not errors, f"约束冲突不该漏给调用方（那是 500 + 丢单）：{errors}"
    assert len(results) == RACERS
    oks, conflicts = _split(results)
    assert len(oks) == 1, f"恰一路该落库，实际 {results}"
    assert oks[0][2] == "pending"
    assert len(conflicts) == RACERS - 1
    assert {(c[1], c[2]) for c in conflicts} == {(409, DUAL_409)}, (
        f"抢输者的回执必须与顺序重复逐字节一致：{conflicts}"
    )

    with Session() as db:
        rows = db.query(DualChannelApp).filter_by(patient_id=pid, drug_name=drug).all()
        assert len(rows) == 1 and rows[0].status == "pending", (
            f"同患者同药品应恰一条，实际 {[(r.id, r.status) for r in rows]}"
        )
        rows[0].status = "rejected"
        db.commit()

    results, errors = _race_on_pg(worker, times=RACERS)
    assert not errors, errors
    oks, conflicts = _split(results)
    assert len(oks) == 1, f"驳回后重申仍应恰一路成功，实际 {results}"
    assert {(c[1], c[2]) for c in conflicts} == {(409, DUAL_409)}

    with Session() as db:
        rows = db.query(DualChannelApp).filter_by(patient_id=pid, drug_name=drug).all()
        assert sorted(r.status for r in rows) == ["pending", "rejected"], (
            f"驳回那条是历史、新的一条待审，实际 {[(r.id, r.status) for r in rows]}"
        )


def test_并发双通道申报_不同药品互不阻塞(pg_engine, actors):
    Session = sessionmaker(bind=pg_engine)
    pid = actors["patient_id"]

    def worker(i):
        with Session() as db:
            user = db.get(User, actors["user_id"])
            try:
                receipt = apply_dual_channel(
                    DualChannelCreate(
                        patient_id=pid, drug_name=f"并发药品-{i}", reason="互不阻塞"
                    ),
                    db=db,
                    user=user,
                )
            except HTTPException as exc:
                return ("409", exc.status_code, exc.detail)
            return ("ok", receipt["id"], receipt["status"])

    results, errors = _race_on_pg(worker, times=RACERS)
    assert not errors, errors
    oks, conflicts = _split(results)
    assert conflicts == [], f"不同药品不该互相拦：{conflicts}"
    assert len(oks) == RACERS

    with Session() as db:
        rows = db.query(DualChannelApp).filter(
            DualChannelApp.patient_id == pid,
            DualChannelApp.drug_name.like("并发药品-%"),
        ).all()
        assert len(rows) == RACERS
