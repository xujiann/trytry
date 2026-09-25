"""同一手术间时段不重叠的真并发取证（P1-117，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_surgery_schedule_races.py -q

为什么非真 PG 不可：排班的重叠判定是「先查同手术间同日有没有重叠的时段、再插一条」——判定读的是**别的行**，
INSERT 不给任何既有行加锁，READ COMMITTED 下八路同时读到「没有重叠」、各插一条。唯一约束
`(room_id, scheduled_date, start_time)` 只挡得住**起点完全相同**的重排，起点错开五分钟的重叠照样两台都排进去。
SQLite 的库级写锁把判定与写入之间的窗口一并锁掉，同一份代码在开发库上永远绿。修法把判定与写入圈进手术间这一行的
`serialized_on`（PG 上 `SELECT … FOR UPDATE`），本档钉的就是这一点。

不变量：八台已审批的手术同时排进同一手术间同一天，起点各错开五分钟、终点都是 10:00（两两重叠、起点互不相同，
撞不上唯一约束）——**恰一台排上，其余七台都是「手术间在 … 已被占用」的 409**，库里这间这天恰一条排班，
其余七台申请仍是「已审批」；异常一次都不许漏给调用方。把 `serialized_on` 拿掉即变红（变异实测见 TECH_DEBT P1-117）。

**这套库是多人共用的**：只用带随机后缀的自建数据，跑完自己收拾，从不 DROP SCHEMA；空库就地补跑迁移（只做加法）。
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

from app.models import (
    Admission,
    Bed,
    OperatingRoom,
    Organization,
    Patient,
    SurgeryRequest,
    SurgerySchedule,
    User,
    Ward,
)
from app.routers.surgery import ScheduleIn, schedule_surgery

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

RACERS = 8
DAY = "2026-10-08"
SERVER_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，任何情况下都不清 schema（理由同 test_insurance_apply_unique_races）。
    池子按参赛路数开并先热身，否则冷池下测出来的是排队而不是并发（见 test_cost_allocation_races）。"""
    engine = create_engine(PG_URL, pool_size=RACERS, max_overflow=RACERS)
    if not all(sa_inspect(engine).has_table(t) for t in ("surgery_schedules", "operating_rooms")):
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
    """一家机构、一名全域角色排班人、一位在院患者、一间手术间、八台已审批的手术申请，名字带随机后缀。"""
    Session = sessionmaker(bind=pg_engine)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG排班并发医院{tag}", org_type="lead_hospital", level="county")
        user = User(username=f"pg_or_{tag}", password_hash="x", full_name=f"并发排班员{tag}", role="director")
        patient = Patient(
            name=f"PG排班并发患者{tag}", id_card=f"3310{uuid.uuid4().int % 10**14:014d}",
            gender="女", birth_date="1970-03-03", ehc_no=f"PG-OR-{tag}",  # 直连建档要自带健康卡号
        )
        db.add_all([org, user, patient])
        db.flush()
        ward = Ward(org_id=org.id, name=f"外科病区{tag}")
        room = OperatingRoom(org_id=org.id, name=f"手术间{tag}")
        db.add_all([ward, room])
        db.flush()
        bed = Bed(ward_id=ward.id, bed_no=f"B{tag}")
        db.add(bed)
        db.flush()
        admission = Admission(patient_id=patient.id, org_id=org.id, ward_id=ward.id, bed_id=bed.id,
                              status="admitted", created_by=user.id)
        db.add(admission)
        db.flush()
        requests = [
            SurgeryRequest(admission_id=admission.id, patient_id=patient.id, org_id=org.id,
                           surgery_name=f"并发排班手术{i}", status="approved", created_by=user.id)
            for i in range(RACERS)
        ]
        db.add_all(requests)
        db.commit()
        ids = {"org_id": org.id, "user_id": user.id, "patient_id": patient.id, "ward_id": ward.id,
               "bed_id": bed.id, "admission_id": admission.id, "room_id": room.id,
               "request_ids": [r.id for r in requests]}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父，created_by / FK 拦着）
        db.query(SurgerySchedule).filter(SurgerySchedule.room_id == ids["room_id"]).delete()
        db.query(SurgeryRequest).filter(SurgeryRequest.id.in_(ids["request_ids"])).delete()
        db.query(Admission).filter_by(id=ids["admission_id"]).delete()
        db.query(Bed).filter_by(id=ids["bed_id"]).delete()
        db.query(OperatingRoom).filter_by(id=ids["room_id"]).delete()
        db.query(Ward).filter_by(id=ids["ward_id"]).delete()
        db.query(Patient).filter_by(id=ids["patient_id"]).delete()
        db.query(User).filter_by(id=ids["user_id"]).delete()
        db.query(Organization).filter_by(id=ids["org_id"]).delete()
        db.commit()


def test_并发排同一手术间起点错开的重叠时段_恰一台排上(pg_engine, world):
    Session = sessionmaker(bind=pg_engine)

    def worker(i):
        with Session() as db:
            user = db.get(User, world["user_id"])
            try:
                receipt = schedule_surgery(
                    world["request_ids"][i],
                    ScheduleIn(room_id=world["room_id"], scheduled_date=DAY,
                               start_time=f"08:{i * 5:02d}", end_time="10:00"),
                    db=db, user=user,
                )
            except HTTPException as exc:
                return ("rejected", exc.status_code, exc.detail)
            return ("ok", receipt["request_id"], receipt["start_time"])

    _warm_pool(pg_engine, RACERS)
    results, errors = _race_on_pg(worker, times=RACERS)
    assert not errors, f"异常不该漏给调用方：{errors}"
    ok = [r for r in results if r[0] == "ok"]
    rejected = [r for r in results if r[0] == "rejected"]
    assert len(ok) == 1, f"应恰一台排上，实际 {results}"
    assert len(rejected) == RACERS - 1
    assert all(code == 409 and "已被占用" in detail for _, code, detail in rejected), rejected
    with Session() as db:
        rows = db.query(SurgerySchedule).filter(
            SurgerySchedule.room_id == world["room_id"], SurgerySchedule.scheduled_date == DAY).all()
        statuses = sorted(
            r.status for r in db.query(SurgeryRequest).filter(SurgeryRequest.id.in_(world["request_ids"])))
    assert len(rows) == 1 and rows[0].request_id == ok[0][1], rows
    assert statuses == ["approved"] * (RACERS - 1) + ["scheduled"], statuses  # 输家的申请原样留在已审批
