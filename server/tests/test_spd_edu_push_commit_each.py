"""宣教推送发一条提交一条（P2-641，第十四批「批处理重跑与幂等」扫描 R2-3）。

定时派发一轮最多 500 条短信 / 微信都在同一个事务里发、由调度器最后一次提交；立即推送一次最多 1000 位患者，也是发完才
提交——中途中断（进程被杀、库连接断、某一条抛错）整批回滚：定时的回到「待发送」，下一轮把已送到患者手机上的再发一遍；
立即的库里一条不剩，清单上看不到发过，再点一次又发一遍。短信通道单条最长等 5 秒，这个事务还能挂着锁半小时以上。
修法：与 ESB 出站逐条提交同一个做法，发一条提交一条——中断只可能重发正在发的那一条。
"""
from datetime import timedelta

import pytest

from app import clock
from app.database import SessionLocal
from app.models import Patient
from app.spd.models import SpdEduMaterial, SpdEduPush


class _Killed(BaseException):
    """模拟进程在发送途中被杀（不是 Exception：调度器的 except Exception 接不住，事务不会走到提交）。"""


@pytest.fixture(scope="module")
def world(client):
    with SessionLocal() as db:
        patients = [Patient(ehc_no=f"EHC-P2641-{i}", name=f"P2641 患者{i}", id_card=f"33010619680808264{i}",
                            phone=f"1370011264{i}") for i in range(6)]
        material = SpdEduMaterial(code="P2641-EDU", title="控糖小贴士", content="少吃精米白面")
        db.add_all([*patients, material])
        db.commit()
        return {"patients": [p.id for p in patients], "material": material.id}


def _sender(monkeypatch, fail_on: int, exc: BaseException):
    from app.spd import platform

    sent: list[str] = []

    def send(phone, content):
        if len(sent) + 1 == fail_on:
            raise exc
        sent.append(phone)
        return True

    monkeypatch.setattr(platform, "send_sms", send)
    return sent


def test_定时派发中途中断_已发的不再重发(world, monkeypatch):
    from app.spd.jobs import spd_edu_push_dispatch

    due_at = (clock.now_local() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    with SessionLocal() as db:
        pushes = [SpdEduPush(material_id=world["material"], patient_id=pid, channel="sms", send_at=due_at)
                  for pid in world["patients"][:3]]
        db.add_all(pushes)
        db.commit()
        ids = [p.id for p in pushes]
    sent = _sender(monkeypatch, fail_on=3, exc=_Killed())
    with pytest.raises(_Killed), SessionLocal() as db:
        spd_edu_push_dispatch(db)
    with SessionLocal() as db:
        assert [db.get(SpdEduPush, i).status for i in ids] == ["sent", "sent", "pending"]   # 修前三条都回到 pending
    resend = _sender(monkeypatch, fail_on=0, exc=_Killed())
    with SessionLocal() as db:
        spd_edu_push_dispatch(db)
        db.commit()
    assert len(sent) + len(resend) == 3, (sent, resend)   # 修前 2 + 3：前两位患者各收到两条


def test_立即推送中途出错_已发的留在清单上(client, admin, world, monkeypatch):
    _sender(monkeypatch, fail_on=3, exc=RuntimeError("库连接断了"))
    targets = world["patients"][3:]
    with pytest.raises(RuntimeError):
        client.post("/api/spd/edu-pushes", headers=admin, json={
            "material_id": world["material"], "patient_ids": targets, "channel": "sms"})
    with SessionLocal() as db:
        rows = db.query(SpdEduPush).filter(SpdEduPush.patient_id.in_(targets)).order_by(SpdEduPush.id).all()
        assert [(r.patient_id, r.status) for r in rows] == [(targets[0], "sent"), (targets[1], "sent")]   # 修前 []
