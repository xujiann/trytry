"""定时宣教派发不看素材停用：约好的推送照样把停用的素材发到患者手机上（P2-252）。

立即推送（`POST /api/spd/edu-pushes`）对停用的素材 404「宣教素材不存在或已停用」；定时派发（`spd_edu_push_dispatch`）
只判了素材在不在——约好的推送到点照样把停用（内容过时、有误而撤下）的素材发出去。同文件的报告推送对停用的报告模板
是跳过的。修法：停用的素材到点不发，推送置失败并写明原因（清单上看得见）；素材不存在的同样写明原因（原先只置失败）。
"""
from datetime import timedelta

import pytest

from app import clock
from app.database import SessionLocal
from app.models import Patient
from app.spd.models import SpdEduMaterial, SpdEduPush


@pytest.fixture(scope="module")
def world(client):
    with SessionLocal() as db:
        patient = Patient(ehc_no="EHC-P2252-01", name="P2252 患者", id_card="330106196808082252",
                          phone="13700112252")
        live = SpdEduMaterial(code="P2252-LIVE", title="控盐小贴士", content="每日食盐不超过 5 克")
        retired = SpdEduMaterial(code="P2252-OFF", title="旧版用药指导", content="已撤下的旧内容", active=False)
        db.add_all([patient, live, retired])
        db.commit()
        return {"patient": patient.id, "live": live.id, "retired": retired.id}


def _due_push(world, material_id):
    with SessionLocal() as db:
        push = SpdEduPush(material_id=material_id, patient_id=world["patient"], channel="sms",
                          send_at=(clock.now_local() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"))
        db.add(push)
        db.commit()
        return push.id


def test_素材停用了到点不发_置失败写明原因(client, world, monkeypatch):
    from app.spd import platform
    from app.spd.jobs import spd_edu_push_dispatch

    sent = []
    monkeypatch.setattr(platform, "send_sms", lambda phone, content: sent.append((phone, content)) or True)
    retired, live = _due_push(world, world["retired"]), _due_push(world, world["live"])
    with SessionLocal() as db:
        spd_edu_push_dispatch(db)
        db.commit()   # 调度框架在任务返回后提交，这里直接调函数、自己提交
    with SessionLocal() as db:
        off = db.get(SpdEduPush, retired)
        assert (off.status, off.fail_reason) == ("failed", "宣教素材已停用，未发送")   # 修前 sent
        assert db.get(SpdEduPush, live).status == "sent"
    assert [content for _phone, content in sent] == ["【健康宣教】控盐小贴士：每日食盐不超过 5 克"]   # 修前两条都发了
