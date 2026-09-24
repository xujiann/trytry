"""远程会诊五个流转端点的归属校验与留痕（P0-31）。

2026-09-24 逐条判 P1-71 名单时取证代理实测：与会诊单毫无关系的第三家机构能受理、拒绝、
**出具会诊意见**（写进申请方读到的那份意见里）、评价、计费（计费 99999 进全县费用合计），
各 200——五个端点只看角色。

先把第三方挡在外面：会诊单带申请方（from_org_id）与受邀方（to_org_id）两个机构列，两方本身
就有服务关系，照常能做。**哪一步该由哪一方做**（模型注释「基层申请、上级接受、出具意见、
申请方评价」从没校验过）属口径问题，另在待裁定清单。
"""
import itertools

import pytest

from app.database import SessionLocal
from app.models import AccessLog, Consultation


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def consult_world(client):
    """甲卫生院申请、丙县医院受邀；乙院与这张单子、这位患者都没有关系。"""
    admin = _login(client, "admin", "admin123")
    orgs = {}
    for key, name in (("a", "会诊归属甲卫生院"), ("b", "会诊归属乙院"), ("c", "会诊归属丙县医院")):
        orgs[key] = client.post("/api/organizations",
                                json={"name": name, "org_type": "township", "level": "township"},
                                headers=admin).json()["id"]
    heads = {}
    for key in ("a", "b", "c"):
        for role in ("doctor", "operator"):
            uname = f"p031_{role}_{key}"
            r = client.post("/api/users",
                            json={"username": uname, "password": "pw123456", "full_name": uname,
                                  "role": role, "org_id": orgs[key]},
                            headers=admin)
            assert r.status_code == 201, r.text
            heads[f"{role}_{key}"] = _login(client, uname)
    patient = client.post("/api/patients",
                          json={"name": "会诊归属患者", "id_card": "320000198707076677"},
                          headers=admin).json()
    seq = itertools.count(1)

    def new_consult(status="applied") -> int:
        db = SessionLocal()
        try:
            row = Consultation(patient_id=patient["id"], from_org_id=orgs["a"], to_org_id=orgs["c"],
                               question=f"会诊问题{next(seq)}", status=status, created_by=1)
            db.add(row)
            db.commit()
            return row.id
        finally:
            db.close()

    return {"h": heads, "patient_id": patient["id"], "new_consult": new_consult}


def _row(consult_id: int) -> tuple:
    db = SessionLocal()
    try:
        c = db.get(Consultation, consult_id)
        return c.status, c.expert_name, c.opinion, c.rating, c.fee_settled
    finally:
        db.close()


def _logs(patient_id: int) -> int:
    db = SessionLocal()
    try:
        return db.query(AccessLog).filter(
            AccessLog.patient_id == patient_id, AccessLog.resource == "consultation"
        ).count()
    finally:
        db.close()


@pytest.mark.parametrize("step, status, body, who", [
    ("accept", "applied", {"expert_name": "乙院专家"}, "doctor_b"),
    ("decline", "applied", None, "doctor_b"),
    ("complete", "accepted", {"opinion": "乙院写的会诊意见"}, "doctor_b"),
    ("rate", "completed", {"rating": 1}, "doctor_b"),
    ("fee", "completed", {"fee": 99999}, "operator_b"),
])
def test_第三方机构碰不了别家的会诊单(client, consult_world, step, status, body, who):
    cid = consult_world["new_consult"](status)
    before = _row(cid)
    r = client.post(f"/api/consultations/{cid}/{step}", json=body, headers=consult_world["h"][who])
    assert r.status_code == 403, (step, r.text)
    assert _row(cid) == before, f"{step} 被拒却落了库"


def test_申请方与受邀方照常走完且留痕(client, consult_world):
    h, pid = consult_world["h"], consult_world["patient_id"]
    cid = consult_world["new_consult"]()
    before = _logs(pid)
    steps = [("accept", {"expert_name": "丙院专家"}, "doctor_c"), ("complete", {"opinion": "建议上转"}, "doctor_c"),
             ("rate", {"rating": 5}, "doctor_a"), ("fee", {"fee": 200}, "operator_c")]
    for step, body, who in steps:
        r = client.post(f"/api/consultations/{cid}/{step}", json=body, headers=h[who])
        assert r.status_code == 200, (step, r.text)
    assert _row(cid) == ("completed", "丙院专家", "建议上转", 5, True)
    assert _logs(pid) == before + len(steps)


def test_先判归属再判状态(client, consult_world):
    cid = consult_world["new_consult"]("completed")
    r = client.post(f"/api/consultations/{cid}/accept", json={"expert_name": "x"},
                    headers=consult_world["h"]["doctor_b"])
    assert r.status_code == 403, "第三方拿到 409 就等于知道了这张单子已经办完"
    r = client.post(f"/api/consultations/{cid}/accept", json={"expert_name": "x"},
                    headers=consult_world["h"]["doctor_c"])
    assert r.status_code == 409, r.text


def test_会诊单不存在照旧404(client, consult_world):
    r = client.post("/api/consultations/987654/decline", headers=consult_world["h"]["doctor_b"])
    assert r.status_code == 404, r.text
