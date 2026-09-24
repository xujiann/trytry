"""术中记录读接口的归属校验与留痕（P0-20）。

`GET /api/surgery/requests/{request_id}/record` 原先连调用方身份都不收——乙院医生（与该患者
毫无业务关系）按申请号就能读甲院的术中记录：实际术式、术者、麻醉、术中所见、并发症、转归、
术前术后诊断（2026-09-24 实测 200）。同一个文件里手术申请清单按机构收口、写接口都校验归属，
只有这一条读接口什么都不问。它是 P1-69（隔一跳挂在患者上的病历表）名单里与 P0-10 / P0-19
同形状的那一类：`surgery_records` 的患者归属在 `surgery_requests.patient_id` 上。

两个方向都钉：无关机构 403；有业务关系的照常 200 并留下 `AccessLog`。
"""
import pytest

from app.database import SessionLocal
from app.models import AccessLog


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def surgery_world(client):
    """甲院走完申请 → 审批 → 排班 → 术中记录；乙院一名医师与该患者毫无关系。"""
    admin = _login(client, "admin", "admin123")
    a = client.post("/api/organizations",
                    json={"name": "术中记录读甲院", "org_type": "lead_hospital", "level": "county"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "术中记录读乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    users = (("srv_doc_a", "doctor", a), ("srv_dir_a", "director", a), ("srv_op_a", "operator", a),
             ("srv_doc_b", "doctor", b))
    for uname, role, org in users:
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": role, "org_id": org["id"]},
                    headers=admin)
    doc_a, dir_a, op_a, doc_b = (_login(client, u) for u, _, _ in users)
    patient = client.post("/api/patients",
                          json={"name": "术中记录读患者", "id_card": "320000199303035679"},
                          headers=admin).json()
    client.post("/api/encounters",
                json={"patient_id": patient["id"], "org_id": a["id"], "encounter_type": "inpatient"},
                headers=doc_a)
    ward = client.post("/api/inpatient/wards",
                       json={"name": "术中记录读病区", "org_id": a["id"]}, headers=admin).json()
    bed = client.post("/api/inpatient/beds",
                      json={"ward_id": ward["id"], "bed_no": "S-01"}, headers=admin).json()
    adm = client.post("/api/inpatient/admissions",
                      json={"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"],
                            "doctor_name": "甲医生", "diagnosis_name": "胆囊结石"},
                      headers=doc_a)
    assert adm.status_code == 201, adm.text
    room = client.post("/api/surgery/rooms", json={"org_id": a["id"], "name": "术中记录读手术间"},
                       headers=admin).json()
    req = client.post("/api/surgery/requests",
                      json={"admission_id": adm.json()["id"], "surgery_name": "腹腔镜胆囊切除术"},
                      headers=doc_a)
    assert req.status_code == 201, req.text
    rid = req.json()["id"]
    assert client.post(f"/api/surgery/requests/{rid}/approve", json={"approved": True},
                       headers=dir_a).status_code == 200
    sched = client.post(f"/api/surgery/requests/{rid}/schedule",
                        json={"room_id": room["id"], "scheduled_date": "2026-09-01",
                              "start_time": "09:00", "end_time": "11:00"},
                        headers=op_a)
    assert sched.status_code == 201, sched.text
    rec = client.post(f"/api/surgery/requests/{rid}/record",
                      json={"actual_surgery_name": "腹腔镜胆囊切除术", "start_at": "2026-09-01 09:10",
                            "end_at": "2026-09-01 10:20", "findings": "胆囊壁增厚", "outcome": "治愈",
                            "postop_diagnosis": "慢性胆囊炎伴结石"},
                      headers=doc_a)
    assert rec.status_code == 201, rec.text
    return {"doc_a": doc_a, "doc_b": doc_b, "patient_id": patient["id"], "request_id": rid}


def test_无关机构按申请号读术中记录403(client, surgery_world):
    r = client.get(f"/api/surgery/requests/{surgery_world['request_id']}/record",
                   headers=surgery_world["doc_b"])
    assert r.status_code == 403, (r.status_code, r.text)


def test_有业务关系的照常能读且留痕(client, surgery_world):
    def count():
        db = SessionLocal()
        try:
            return db.query(AccessLog).filter(
                AccessLog.patient_id == surgery_world["patient_id"], AccessLog.resource == "surgery_record"
            ).count()
        finally:
            db.close()

    before = count()
    r = client.get(f"/api/surgery/requests/{surgery_world['request_id']}/record",
                   headers=surgery_world["doc_a"])
    assert r.status_code == 200, r.text
    assert r.json()["postop_diagnosis"] == "慢性胆囊炎伴结石"
    assert count() == before + 1, "放行了却没留痕"


def test_申请号不存在照旧404(client, surgery_world):
    r = client.get("/api/surgery/requests/987654/record", headers=surgery_world["doc_b"])
    assert r.status_code == 404
