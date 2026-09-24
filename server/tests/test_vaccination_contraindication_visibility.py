"""接种禁忌「解除」的归属校验与留痕（P0-25）。

同文件的禁忌清单（`GET /contraindications`）与接种前评估（`GET /pre-check`）早就按患者
可见性判定并留痕（`resource="vaccination"`），解除这一处没跟上——2026-09-24 按 P1-71
名单实测：乙院医生按禁忌号 `POST /contraindications/{id}/lift`，甲院患者的禁忌就解除了
（200），接种前评估从此不再因这一条拦截。乙院连这条禁忌都看不到（清单 403），却能把它解掉。

照同文件口径补上：按禁忌所属患者判可见性并留痕；先判归属再判状态，403 不泄露
"这条禁忌解没解"。两个方向都钉。
"""
import itertools

import pytest

from app.database import SessionLocal
from app.models import AccessLog, VaccineContraindication


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def contra_world(client):
    """甲院接诊过的患者登记着接种禁忌；乙院一名医师与该患者毫无关系。"""
    admin = _login(client, "admin", "admin123")
    a = client.post("/api/organizations",
                    json={"name": "禁忌归属甲院", "org_type": "lead_hospital", "level": "county"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "禁忌归属乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    for uname, org in (("vacc_doc_a", a), ("vacc_doc_b", b)):
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": "doctor", "org_id": org["id"]},
                    headers=admin)
    doc_a, doc_b = _login(client, "vacc_doc_a"), _login(client, "vacc_doc_b")
    patient = client.post("/api/patients",
                          json={"name": "禁忌归属患者", "id_card": "320000199412126673"},
                          headers=admin).json()
    enc = client.post("/api/encounters",
                      json={"patient_id": patient["id"], "org_id": a["id"], "encounter_type": "outpatient"},
                      headers=doc_a)
    assert enc.status_code == 201, enc.text
    seq = itertools.count(1)

    def new_contra(status="active") -> int:
        db = SessionLocal()
        try:
            row = VaccineContraindication(patient_id=patient["id"], vaccine_code=f"VAC{next(seq)}",
                                          reason="急性发热", status=status)
            db.add(row)
            db.commit()
            return row.id
        finally:
            db.close()

    return {"doc_a": doc_a, "doc_b": doc_b, "patient_id": patient["id"], "new_contra": new_contra}


def _vacc_logs(patient_id: int) -> int:
    db = SessionLocal()
    try:
        return db.query(AccessLog).filter(
            AccessLog.patient_id == patient_id, AccessLog.resource == "vaccination"
        ).count()
    finally:
        db.close()


def _status(contra_id: int) -> str:
    db = SessionLocal()
    try:
        return db.get(VaccineContraindication, contra_id).status
    finally:
        db.close()


def test_无关机构不能解除别家患者的接种禁忌(client, contra_world):
    cid = contra_world["new_contra"]()
    r = client.post(f"/api/vaccination/contraindications/{cid}/lift",
                    json={"lift_reason": "乙院解除"}, headers=contra_world["doc_b"])
    assert r.status_code == 403, r.text
    assert _status(cid) == "active", "被拒的解除不能落库：接种前评估还得继续拦"


def test_有业务关系的照常能解且留痕(client, contra_world):
    cid = contra_world["new_contra"]()
    before = _vacc_logs(contra_world["patient_id"])
    r = client.post(f"/api/vaccination/contraindications/{cid}/lift",
                    json={"lift_reason": "复测体温已正常"}, headers=contra_world["doc_a"])
    assert r.status_code == 200, r.text
    assert _status(cid) == "lifted"
    assert _vacc_logs(contra_world["patient_id"]) == before + 1


def test_先判归属再判状态_403不泄露解没解(client, contra_world):
    cid = contra_world["new_contra"](status="lifted")
    r = client.post(f"/api/vaccination/contraindications/{cid}/lift",
                    json={"lift_reason": "乙院再解"}, headers=contra_world["doc_b"])
    assert r.status_code == 403, "无关机构拿到 409 就等于知道了这条禁忌已经解除"
    r = client.post(f"/api/vaccination/contraindications/{cid}/lift",
                    json={"lift_reason": "甲院再解"}, headers=contra_world["doc_a"])
    assert r.status_code == 409, r.text


def test_禁忌号不存在照旧404(client, contra_world):
    r = client.post("/api/vaccination/contraindications/987654/lift",
                    json={"lift_reason": "x"}, headers=contra_world["doc_b"])
    assert r.status_code == 404, r.text
