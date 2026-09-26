"""状态机「推进」与同一状态机其余迁移的真并发取证（P2-317，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_status_flow_transition_races.py -q

同伴 test_status_flow_transition.py 在 SQLite 上用确定时序钉逻辑（读到之后、写入之前插进另一路的提交）；这里钉 PG 的
READ COMMITTED 下真并发的不变量：**每一路成功都真的把行往前推了一步**——成功几路，行就走了几步，其余 409。修前八路都读到
同一个旧状态、各自写回同一个下一态：八路全 200，行只走了一步，库里只剩最后提交的那份（发给了哪家、结论是什么、标本是
拒收还是核收，看谁最后提交），先提交的几路拿到的回执全是假的。

**不断言「恰一路成功」**（第六十七轮真 PG 实测纠正）：Barrier 之下多数路同时读到起点，但总有路在别人提交之后才读到行，
它接着合法地走下一步（顺序请求本来就能连点几步：已调配 → 已煎煮；已配送之后再取消）。只能走一步的（响应申领、核收 /
拒收）才恰一路成功。

条件 UPDATE 在 PG 上为什么成立：`UPDATE … WHERE id = ? AND status = ?` 撞上别的事务已改未提交的行会等它提交，再按新版本
重判 WHERE——判定与写入在同一条语句里，窗口是关上的。

**这套库是多人共用的**：只用带随机后缀的自建数据，跑完自己收拾（先子后父），从不 DROP SCHEMA；空库就地补跑迁移（只做加法）。
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

from test_disease_enrollment_unique_races import _warm_pool  # 连接池热身，理由见 test_cost_allocation_races
from test_postgres_real import _race_on_pg  # Barrier 真并发的既有夹具，不另造一份

from app.models import (CssdRequest, DrugShortage, EmergencyCase, ExamRequest, Organization, PathologySpecimen, Patient,
                        SterilizationBatch, TcmDispenseOrder, User)
from app.routers import cssd, emergency, exams, medication, pathology, tcm

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

RACERS = 8
SERVER_DIR = Path(__file__).resolve().parents[1]
TABLES = ("sterilization_batches", "cssd_requests", "emergency_cases", "exam_requests", "drug_shortages",
          "pathology_specimens", "tcm_dispense_orders")

#: 全域角色：直调路由函数时过机构写权限（不落库，只给归属校验看角色）
ADMIN = User(username="pg-p2317", role="admin", org_id=None)


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，任何情况下都不清 schema（理由同 test_insurance_apply_unique_races）。
    池子按参赛路数开并先热身，否则冷池下测出来的是排队而不是并发（见 test_cost_allocation_races）。"""
    engine = create_engine(PG_URL, pool_size=RACERS, max_overflow=RACERS)
    if not all(sa_inspect(engine).has_table(t) for t in TABLES):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR, env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"迁移在 PG 上失败：\n{result.stderr[-2000:]}"
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def world(pg_engine):
    """两家机构（发放的两个去处）、一名开单人、一位患者；各用例自己造要抢的那一行，id 记进来，结束时一并收拾。"""
    Session = sessionmaker(bind=pg_engine)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        orgs = [Organization(name=f"PG状态机并发院{tag}-{i}", org_type="township", level="township") for i in range(2)]
        patient = Patient(name=f"PG状态机患者{tag}", id_card=f"3312{uuid.uuid4().int % 10**14:014d}", gender="女",
                          birth_date="1970-07-07", ehc_no=f"PG-P2317-{tag}")
        db.add_all([*orgs, patient])
        db.flush()
        user = User(username=f"pg_p2317_{tag}", password_hash="x", full_name=f"开单人{tag}", role="doctor",
                    org_id=orgs[0].id)
        db.add(user)
        db.commit()
        ids = {"tag": tag, "orgs": [o.id for o in orgs], "patient": patient.id, "user": user.id,
               **{model.__name__: [] for model in (SterilizationBatch, CssdRequest, EmergencyCase, ExamRequest,
                                                   DrugShortage, PathologySpecimen, TcmDispenseOrder)}}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父）
        for model in (PathologySpecimen, CssdRequest, SterilizationBatch, EmergencyCase, ExamRequest, DrugShortage,
                      TcmDispenseOrder):
            db.query(model).filter(model.id.in_(ids[model.__name__])).delete(synchronize_session=False)
        db.query(User).filter_by(id=ids["user"]).delete()
        db.query(Patient).filter_by(id=ids["patient"]).delete()
        db.query(Organization).filter(Organization.id.in_(ids["orgs"])).delete(synchronize_session=False)
        db.commit()


def _make(pg_engine, world, row):
    with sessionmaker(bind=pg_engine)() as db:
        db.add(row)
        db.commit()
        world[type(row).__name__].append(row.id)
        return row.id


def _race(pg_engine, step):
    """八路同时走 `step(i, db)`：返回 (成功的几路的 (i, 返回值), 被拒的几路的 (状态码, 文案))。

    这里只断言「不漏异常、被拒的一律 409」；成功几路由各用例按自己的流转表断言（见模块说明）。"""
    Session = sessionmaker(bind=pg_engine)

    def worker(i):
        with Session() as db:
            try:
                return ("ok", (i, step(i, db)))
            except HTTPException as exc:
                return ("rejected", (exc.status_code, exc.detail))

    _warm_pool(pg_engine, RACERS)
    results, errors = _race_on_pg(worker, times=RACERS)
    assert not errors, f"异常不该漏给调用方：{errors}"
    ok = [r[1] for r in results if r[0] == "ok"]
    rejected = [r[1] for r in results if r[0] == "rejected"]
    assert ok, f"一路都没走到：{results}"
    assert all(code == 409 for code, _ in rejected), rejected
    return ok, rejected


def _steps_match(flow, start, final, ok):
    """成功几路，行就从起点往后走了几步。修前八路都读到起点、各写一遍下一态：八路 200，行只走了一步。"""
    assert flow.index(final) - flow.index(start) == len(ok), f"起点 {start}、终点 {final}，成功 {len(ok)} 路：{ok}"


def _get(pg_engine, model, row_id):
    with sessionmaker(bind=pg_engine)() as db:
        row = db.get(model, row_id)
        db.expunge(row)
        return row


def test_八路同时发放同一批次_成功几路就走几步_接收机构是发出那一路选的(pg_engine, world):
    batch = _make(pg_engine, world, SterilizationBatch(
        batch_no=f"PG2317-{world['tag']}", center_org_id=world["orgs"][0], item_name="器械包", quantity=5,
        status="sterile"))
    ok, _ = _race(pg_engine, lambda i, db: cssd.advance(
        batch, dispatched_to_org_id=world["orgs"][i % 2], db=db, user=ADMIN).status)
    row = _get(pg_engine, SterilizationBatch, batch)
    _steps_match(["sterile", "dispatched", "recycled"], "sterile", row.status, ok)   # 修前：八路 200、只发出一次
    # 发出只有一路（其后的至多是「回收」，不改去向）：库里的接收机构是某一路成功者选的
    assert row.dispatched_to_org_id in {world["orgs"][i % 2] for i, _ in ok}


def test_八路同时响应同一申领_恰一路_批次是它选的(pg_engine, world):
    batches = [_make(pg_engine, world, SterilizationBatch(
        batch_no=f"PG2317-{world['tag']}-{i}", center_org_id=world["orgs"][0], item_name="器械包", quantity=5,
        status="sterile")) for i in range(2)]
    req = _make(pg_engine, world, CssdRequest(org_id=world["orgs"][1], item_name="换药包", quantity=2))
    ok, rejected = _race(pg_engine, lambda i, db: cssd.fulfill_cssd_request(
        req, batch_id=batches[i % 2], db=db, user=ADMIN)["batch_id"])
    # 响应申领只有一步：恰一路成功，库里的批次是它选的
    assert len(ok) == 1, ok   # 修前：八路 200，批次是最后提交的那一路的
    assert {detail for _, detail in rejected} == {"申领已处理"}
    row = _get(pg_engine, CssdRequest, req)
    assert (row.status, row.batch_id) == ("fulfilled", ok[0][1])


@pytest.mark.parametrize("case", ["emergency", "sample", "tcm"])
def test_八路同时推进同一行_成功几路就走几步(pg_engine, world, case):
    """修前八路都读到起点、各写一遍下一态：八路 200，行只走了一步。"""
    if case == "emergency":
        row_id = _make(pg_engine, world, EmergencyCase(location=f"PG2317 路口 {world['tag']}"))
        ok, _ = _race(pg_engine, lambda i, db: emergency.advance(row_id, db=db))
        _steps_match(["dispatched", "en_route", "arrived", "admitted"], "dispatched",
                     _get(pg_engine, EmergencyCase, row_id).status, ok)
    elif case == "sample":
        row_id = _make(pg_engine, world, ExamRequest(
            patient_id=world["patient"], from_org_id=world["orgs"][0], center_type="lab", item_code="PG2317",
            item_name="血常规", created_by=world["user"], sample_status="collected"))
        ok, _ = _race(pg_engine, lambda i, db: exams.advance_sample(row_id, db=db))
        _steps_match(["collected", "in_transit", "received"], "collected",
                     _get(pg_engine, ExamRequest, row_id).sample_status, ok)
    else:
        row_id = _make(pg_engine, world, TcmDispenseOrder(
            patient_id=world["patient"], from_org_id=world["orgs"][0], herbs="黄芪30g", doses=7, decoct=True))
        ok, _ = _race(pg_engine, lambda i, db: tcm.advance_order(row_id, db=db))
        _steps_match(["ordered", "dispensed", "decocted", "delivering", "delivered"], "ordered",
                     _get(pg_engine, TcmDispenseOrder, row_id).status, ok)


def test_缺药登记推进与取消同时到_只出合法的先后_状态对得上(pg_engine, world):
    shortage = _make(pg_engine, world, DrugShortage(
        org_id=world["orgs"][0], drug_code=f"PG{world['tag']}", drug_name="胰岛素", quantity=3, status="purchasing"))

    def step(i, db):
        if i % 2:
            return medication.close_shortage(shortage, medication.ShortageClose(result="cancelled"), db=db,
                                              user=ADMIN).status
        return medication.advance_shortage(shortage, db=db, user=ADMIN).status

    ok, _ = _race(pg_engine, step)
    # 合法的只有三种：只取消了；只配送了；先配送、后取消（取消可以在任何阶段发生）。已取消之后不能再推进
    outcomes = sorted(v for _, v in ok)
    assert outcomes in (["cancelled"], ["delivered"], ["cancelled", "delivered"]), ok   # 修前：推进与取消各成好几路
    assert _get(pg_engine, DrugShortage, shortage).status == ("cancelled" if "cancelled" in outcomes else "delivered")


def test_标本核收与拒收同时到_只成一路_状态对得上(pg_engine, world):
    request = _make(pg_engine, world, ExamRequest(
        patient_id=world["patient"], from_org_id=world["orgs"][0], center_type="pathology", item_code="PG2317P",
        item_name="病理", created_by=world["user"]))
    specimen = _make(pg_engine, world, PathologySpecimen(request_id=request, specimen_no=f"PG2317-{world['tag']}"))

    def step(i, db):
        if i % 2:
            return pathology.reject_specimen(specimen, pathology.SpecimenReject(reject_reason="标本量不足"), db=db)["status"]
        return pathology.receive_specimen(specimen, pathology.SpecimenReceive(received_by=f"核收员{i}"), db=db)["status"]

    ok, _ = _race(pg_engine, step)
    # 核收与拒收都只收「待核收」：恰一路成功
    assert len(ok) == 1, ok   # 修前：核收与拒收都 200，拒收原因挂在已核收的标本上
    winner = ok[0][1]
    row = _get(pg_engine, PathologySpecimen, specimen)
    assert row.status == winner
    assert (row.reject_reason != "") == (winner == "rejected") and (row.received_by != "") == (winner == "received")
