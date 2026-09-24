"""慢专病复诊计划的归属校验与留痕（P0-24）。

同组「建计划」与「按患者查看板」早就按患者可见性判定并留痕（`resource="spd_revisit"`），
另外两处没跟上——2026-09-24 按 P1-71 名单实测：

- `PATCH /api/spd/revisits/{id}` 连调用方身份都不收：乙院医生按计划号就能把甲院患者的
  复诊计划改期、办结、移除（200），计划日志里只记一句"状态变更为 removed"，看不出是谁；
- `GET /api/spd/revisits` 只在带 `patient_id` 时判可见性，不带就列出全域全部复诊计划，
  带患者姓名、复查项目与计划日期。

照同组口径补上：改计划按其所属患者判可见性并留痕；看板走 `scope_patient_list`
（全域角色不过滤，其余只见本机构服务过的患者）。两个方向都钉。
"""
import pytest

from app.database import SessionLocal
from app.models import AccessLog
from app.spd.models import SpdRevisit


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def revisit_world(client):
    """甲院接诊过的患者有复诊计划；乙院一名医师与该患者毫无关系。"""
    admin = _login(client, "admin", "admin123")
    a = client.post("/api/organizations",
                    json={"name": "复诊归属甲院", "org_type": "lead_hospital", "level": "county"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "复诊归属乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    for uname, org in (("spdr_doc_a", a), ("spdr_doc_b", b)):
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": "doctor", "org_id": org["id"]},
                    headers=admin)
    doc_a, doc_b = _login(client, "spdr_doc_a"), _login(client, "spdr_doc_b")
    patient = client.post("/api/patients",
                          json={"name": "复诊归属患者", "id_card": "320000199808086675"},
                          headers=admin).json()
    enc = client.post("/api/encounters",
                      json={"patient_id": patient["id"], "org_id": a["id"], "encounter_type": "outpatient"},
                      headers=doc_a)
    assert enc.status_code == 201, enc.text

    def new_revisit() -> int:
        db = SessionLocal()
        try:
            row = SpdRevisit(patient_id=patient["id"], program_code="hypertension",
                             plan_date="2026-10-08", items="复查血压", log=[])
            db.add(row)
            db.commit()
            return row.id
        finally:
            db.close()

    return {"doc_a": doc_a, "doc_b": doc_b, "admin": admin, "patient_id": patient["id"],
            "new_revisit": new_revisit}


def _revisit_logs(patient_id: int) -> int:
    db = SessionLocal()
    try:
        return db.query(AccessLog).filter(
            AccessLog.patient_id == patient_id, AccessLog.resource == "spd_revisit"
        ).count()
    finally:
        db.close()


def _row(revisit_id: int) -> SpdRevisit:
    db = SessionLocal()
    try:
        row = db.get(SpdRevisit, revisit_id)
        db.expunge(row)
        return row
    finally:
        db.close()


def test_无关机构不能改别家患者的复诊计划(client, revisit_world):
    rid = revisit_world["new_revisit"]()
    r = client.patch(f"/api/spd/revisits/{rid}", json={"status": "removed", "note": "乙院移除"},
                     headers=revisit_world["doc_b"])
    assert r.status_code == 403, r.text
    row = _row(rid)
    assert (row.status, row.log) == ("planned", []), "被拒的改动不能落库，日志也不能多一条"


def test_有业务关系的照常能改且留痕(client, revisit_world):
    rid = revisit_world["new_revisit"]()
    before = _revisit_logs(revisit_world["patient_id"])
    r = client.patch(f"/api/spd/revisits/{rid}", json={"plan_date": "2026-10-15", "note": "患者外出，改期一周"},
                     headers=revisit_world["doc_a"])
    assert r.status_code == 200, r.text
    assert _row(rid).plan_date == "2026-10-15"
    assert _revisit_logs(revisit_world["patient_id"]) == before + 1


def test_复诊看板按可见患者收口(client, revisit_world):
    rid = revisit_world["new_revisit"]()

    def ids(headers):
        r = client.get("/api/spd/revisits?limit=500", headers=headers)
        assert r.status_code == 200, r.text
        return {row["id"] for row in r.json()}

    assert rid not in ids(revisit_world["doc_b"]), "乙院看得见甲院患者的复诊计划（带姓名）"
    assert rid in ids(revisit_world["doc_a"])
    assert rid in ids(revisit_world["admin"])


def test_按患者查看板仍然判定并留痕(client, revisit_world):
    pid = revisit_world["patient_id"]
    r = client.get(f"/api/spd/revisits?patient_id={pid}", headers=revisit_world["doc_b"])
    assert r.status_code == 403, r.text
    before = _revisit_logs(pid)
    assert client.get(f"/api/spd/revisits?patient_id={pid}", headers=revisit_world["doc_a"]).status_code == 200
    assert _revisit_logs(pid) == before + 1


def test_计划号不存在照旧404(client, revisit_world):
    r = client.patch("/api/spd/revisits/987654", json={"status": "done"}, headers=revisit_world["doc_b"])
    assert r.status_code == 404, r.text
