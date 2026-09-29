"""病种停用后，停用前递交的居民服务申请只能驳回（P2-762，第二十批「停用 / 注销 / 作废的对象仍在被用」扫描 M3-3）。

`service.unknown_program` 写着「新纳入（居民自查筛查、申请加入）不收停用的病种，与建档 / 筛查同一口径（P1-89）」，居民
递交入口按它拒停用病种；受理就是把人新纳入目标池，原先却不查：停用前递交的申请照样受理 200，人进了目标池、居民端显示
「已受理」等签约，按受理结果签约建档却 404「专病档案不存在或已停用」——目标池里多一个永远建不了档的人。
"""
import pytest

from app.database import SessionLocal
from app.spd.models import SpdCandidate, SpdProgram, SpdServiceApply

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    patients = [client.post("/api/patients", headers=admin, json={
        "name": f"P2762 患者{n}", "id_card": f"33010219500101{2761 + n:04d}"}).json()["id"] for n in (1, 2)]
    with SessionLocal() as db:
        db.add(SpdProgram(code="p2762_prog", name="P2762 病种", category="chronic", active=True))
        applies = [SpdServiceApply(patient_id=p, program_code="p2762_prog", note="我想加入", status="pending")
                   for p in patients]
        db.add_all(applies)
        db.commit()
        ids = [a.id for a in applies]
        db.query(SpdProgram).filter_by(code="p2762_prog").update({"active": False})   # 递交之后病种停用
        db.commit()
    return {"patients": patients, "applies": ids}


def _state(apply_id, patient_id):
    with SessionLocal() as db:
        status = db.get(SpdServiceApply, apply_id).status
        pool = db.query(SpdCandidate).filter_by(patient_id=patient_id, program_code="p2762_prog").count()
        return status, pool


def test_停用病种的申请受理_409_仍待受理_目标池无新增(client, admin, world):
    apply, patient = world["applies"][0], world["patients"][0]
    resp = client.post(f"{B}/service-applies/{apply}/handle", headers=admin,
                       json={"status": "accepted", "handle_note": "受理"})
    assert resp.status_code == 409, resp.text   # 修前 200：人进了目标池，签约建档却 404
    assert resp.json()["detail"] == "专病档案不存在或已停用，只能驳回"
    assert _state(apply, patient) == ("pending", 0)


def test_停用病种的申请照常能驳回(client, admin, world):
    apply, patient = world["applies"][1], world["patients"][1]
    resp = client.post(f"{B}/service-applies/{apply}/handle", headers=admin,
                       json={"status": "rejected", "handle_note": "该病种已停止服务"})
    assert resp.status_code == 200 and resp.json() == {"id": apply, "status": "rejected"}, resp.text
    assert _state(apply, patient) == ("rejected", 0)
