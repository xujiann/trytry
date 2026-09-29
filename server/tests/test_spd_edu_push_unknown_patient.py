"""宣教推送的患者号先全部查过存在再发（P2-726，第十九批「批量 vs 单条」扫描 K1-4）。

全域角色过可见性守卫不查存在（P2-52）。立即推送混进一个不存在的患者号：前面几位的短信已经发出、已经逐条提交（P2-641），
发到错号那一条才撞外键 500，回执没有；经办以为没发出去，去掉错号再点一次，前面的人收两遍。同子系统批量加分组早就先查
存在、404 点名。修法：与批量加分组同一顺序，先查存在（404 点名，一条都不发）再判可见性——不给不存在的号写调阅留痕
（写不进去，只落一条「留痕丢失」的错误日志）。
"""
import pytest

from app.database import SessionLocal
from app.models import Patient
from app.spd.models import SpdEduMaterial, SpdEduPush

MISSING = 987654321


@pytest.fixture(scope="module")
def world(client):
    with SessionLocal() as db:
        patients = [Patient(ehc_no=f"EHC-P2726-{i}", name=f"P2726 患者{i}", id_card=f"33010619680808272{i}",
                            phone=f"1370011272{i}") for i in range(2)]
        material = SpdEduMaterial(code="P2726-EDU", title="低盐饮食", content="每日盐不超过5克")
        db.add_all([*patients, material])
        db.commit()
        return {"patients": [p.id for p in patients], "material": material.id}


@pytest.fixture
def sent(monkeypatch):
    from app.spd import platform

    phones: list[str] = []
    monkeypatch.setattr(platform, "send_sms", lambda phone, content: phones.append(phone) or True)
    return phones


def _push(client, headers, world, patient_ids, **extra):
    return client.post("/api/spd/edu-pushes", headers=headers, json={
        "material_id": world["material"], "patient_ids": patient_ids, "channel": "sms", **extra})


def _pushes(world):
    with SessionLocal() as db:
        return db.query(SpdEduPush).filter(SpdEduPush.material_id == world["material"]).count()


@pytest.mark.parametrize("send_at", ["", "2099-01-01 08:00:00"], ids=["立即推送", "定时推送"])
def test_混进不存在的患者号_404点名_一条都不发(client, admin, world, sent, send_at):
    first, second = world["patients"]
    resp = _push(client, admin, world, [first, MISSING, second], send_at=send_at)
    assert resp.status_code == 404 and str(MISSING) in resp.json()["detail"], resp.text   # 修前 500
    assert sent == [] and _pushes(world) == 0   # 修前立即推送：第一位已发出、已落库


def test_去掉错号重发_每人只收一条(client, admin, world, sent):
    resp = _push(client, admin, world, world["patients"])
    assert resp.status_code == 201 and resp.json()["sent"] == 2, resp.text
    assert sorted(sent) == ["13700112720", "13700112721"]   # 修前第一位收两遍


def test_不存在的号不去写调阅留痕(client, admin, world, sent, caplog):
    resp = _push(client, admin, world, [MISSING])
    assert resp.status_code == 404, resp.text
    assert "调阅留痕写入失败" not in caplog.text   # 修前先判可见性：给不存在的号写留痕撞外键，落一条错误日志
