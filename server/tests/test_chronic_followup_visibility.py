"""慢病随访「记一次」的归属校验与留痕（P0-26）。

同文件的风险评分（`GET /{id}/risk`）与随访记录（`GET /{id}/followups`）早就按患者可见性
判定并留痕（`resource="chronic"`），记随访这一处没跟上——2026-09-24 按 P1-71 名单实测：
乙院医生按档案号 `POST /api/chronic/{id}/followups` 就给甲院管着的患者记了一次随访（201），
还顺带改掉了档案的**分级**与**下次随访日**（随访中心的超期名单与分级管理都按它们算）。

照同文件口径补上：按档案所属患者判可见性并留痕。管这份档案的机构本身就有服务关系
（`managed_by_org_id` 是机构外键），照常能记。
"""
import pytest

from app.database import SessionLocal
from app.models import AccessLog, ChronicPatient, FollowUp


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def chronic_world(client):
    """甲院管着一位高血压患者的慢病档案；乙院一名医师与该患者毫无关系。"""
    admin = _login(client, "admin", "admin123")
    a = client.post("/api/organizations",
                    json={"name": "慢病归属甲院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "慢病归属乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    for uname, org in (("chr_doc_a", a), ("chr_doc_b", b)):
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": "doctor", "org_id": org["id"]},
                    headers=admin)
    patient = client.post("/api/patients",
                          json={"name": "慢病归属患者", "id_card": "320000199303036671"},
                          headers=admin).json()

    def new_chronic(disease: str) -> int:
        # 同一患者同一病种只能建一份档案（唯一约束），每条用例各用一个病种
        db = SessionLocal()
        try:
            row = ChronicPatient(patient_id=patient["id"], disease=disease,
                                 managed_by_org_id=a["id"], level=1, next_due="2026-10-01")
            db.add(row)
            db.commit()
            return row.id
        finally:
            db.close()

    return {"doc_a": _login(client, "chr_doc_a"), "doc_b": _login(client, "chr_doc_b"),
            "patient_id": patient["id"], "new_chronic": new_chronic}


def _chronic_logs(patient_id: int) -> int:
    db = SessionLocal()
    try:
        return db.query(AccessLog).filter(
            AccessLog.patient_id == patient_id, AccessLog.resource == "chronic"
        ).count()
    finally:
        db.close()


def _state(chronic_id: int) -> tuple:
    db = SessionLocal()
    try:
        row = db.get(ChronicPatient, chronic_id)
        followups = db.query(FollowUp).filter(FollowUp.chronic_id == chronic_id).count()
        return row.level, row.next_due, followups
    finally:
        db.close()


def test_无关机构不能给别家管着的患者记随访(client, chronic_world):
    cid = chronic_world["new_chronic"]("hypertension")
    r = client.post(f"/api/chronic/{cid}/followups", json={"sbp": 185, "dbp": 110, "next_due": "2027-01-01"},
                    headers=chronic_world["doc_b"])
    assert r.status_code == 403, r.text
    assert _state(cid) == (1, "2026-10-01", 0), "被拒的随访不能落库，分级与下次随访日也不能动"


def test_管这份档案的机构照常能记且留痕(client, chronic_world):
    cid = chronic_world["new_chronic"]("diabetes")
    before = _chronic_logs(chronic_world["patient_id"])
    r = client.post(f"/api/chronic/{cid}/followups", json={"sbp": 132, "dbp": 84, "next_due": "2026-12-01"},
                    headers=chronic_world["doc_a"])
    assert r.status_code == 201, r.text
    assert _state(cid)[1:] == ("2026-12-01", 1)
    assert _chronic_logs(chronic_world["patient_id"]) == before + 1


def test_档案号不存在照旧404(client, chronic_world):
    r = client.post("/api/chronic/987654/followups", json={"sbp": 120}, headers=chronic_world["doc_b"])
    assert r.status_code == 404, r.text
