"""慢专病宣教推送的时点按本地时间：定时推送到点即发、立即推送记本地时刻（P2-215）。

`send_at` 是页面上 datetime-local 手填的本地时间，定时派发却拿 UTC 去比——东八区约好晚上 8 点推的宣教，要到次日
凌晨 4 点才发；立即推送缺省记的是 UTC，10 点推的在清单上印「02:00」，与手填的定时推送不在同一把尺子上。
`clock.now_local` 就是给这类「给人看的时间」用的（P2-171 同一处理）。报告推送的「08:00」时点同理改按本地钟点比。
"""
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.database import SessionLocal
from app.models import Patient
from app.spd.models import SpdEduMaterial, SpdEduPush


@pytest.fixture
def shanghai():
    """把进程时区拨到东八区，用完拨回去——本地与 UTC 差 8 小时，取错了一眼看得出。"""
    saved = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


def _beijing_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)


@pytest.fixture(scope="module")
def world(client):
    with SessionLocal() as db:
        patient = Patient(ehc_no="EHC-P2215-01", name="P2215 患者", id_card="330106196707071677")   # 没有手机号
        material = SpdEduMaterial(code="P2215-EDU", title="低盐饮食", content="每日食盐不超过 5 克")
        db.add_all([patient, material])
        db.commit()
        return {"patient": patient.id, "material": material.id}


def test_定时推送按本地时间到点即发(client, world, shanghai):
    from app.spd.jobs import spd_edu_push_dispatch

    with SessionLocal() as db:
        push = SpdEduPush(material_id=world["material"], patient_id=world["patient"], channel="sms",
                          send_at=(_beijing_now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"))
        db.add(push)
        db.commit()
        push_id = push.id
        spd_edu_push_dispatch(db)
        db.commit()   # 调度框架在任务返回后提交，这里直接调函数、自己提交
    with SessionLocal() as db:
        # 没手机号 → failed，但关键是已经派发过（修前本地 5 分钟前的时点比 UTC 晚 8 小时，一直 pending）
        assert db.get(SpdEduPush, push_id).status == "failed"


def test_立即推送记本地时刻(client, admin, world, shanghai):
    resp = client.post("/api/spd/edu-pushes", headers=admin, json={
        "material_id": world["material"], "patient_ids": [world["patient"]], "channel": "sms"})
    assert resp.status_code == 201, resp.text
    with SessionLocal() as db:
        latest = db.query(SpdEduPush).order_by(SpdEduPush.id.desc()).first()
        recorded = datetime.strptime(latest.send_at, "%Y-%m-%d %H:%M:%S")
    assert abs(recorded - _beijing_now()) < timedelta(minutes=5), recorded     # 修前差 8 小时（记的是 UTC）
