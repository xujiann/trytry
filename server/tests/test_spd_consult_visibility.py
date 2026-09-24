"""慢专病在线咨询三个端点的归属校验与留痕（P0-23）。

同一组端点里，`reply_consult` 与 `consult_messages` 早就按会话所属患者做可见性判定并留痕
（`assert_patient_visible(…, resource="spd_consult")`），另外三个没跟上——2026-09-24 实测：

- `POST /api/spd/consults/{id}/close` 连调用方身份都不收：乙院医生按会话号就能把甲院患者
  正在进行的咨询关掉（200）；
- `POST /api/spd/consults/{id}/to-followup` 不看会话属于谁：乙院账号能给甲院患者派一条
  随访任务（201，任务还挂在乙院名下）；
- `GET /api/spd/consults` 收了调用方却不按它收口：任一职员账号都能列出全域全部会话，
  带患者姓名与病种。

照同组两个端点的口径补上：按会话所属患者判可见性并留痕；清单走 `scope_patient_list`
（全域角色不过滤，其余只见本机构服务过的患者）。两个方向都钉：无关机构 403 / 看不见；
有业务关系的照常能做并留下 `AccessLog`。
"""
import itertools

import pytest

from app.database import SessionLocal
from app.models import AccessLog
from app.spd.models import SpdConsult


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def consult_world(client):
    """甲院接诊过的患者开着一条慢专病咨询；乙院一名医师与该患者毫无关系。"""
    admin = _login(client, "admin", "admin123")
    a = client.post("/api/organizations",
                    json={"name": "咨询归属甲院", "org_type": "lead_hospital", "level": "county"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "咨询归属乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    for uname, org in (("spdc_doc_a", a), ("spdc_doc_b", b)):
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": "doctor", "org_id": org["id"]},
                    headers=admin)
    doc_a, doc_b = _login(client, "spdc_doc_a"), _login(client, "spdc_doc_b")
    patient = client.post("/api/patients",
                          json={"name": "咨询归属患者", "id_card": "320000199707076674"},
                          headers=admin).json()
    enc = client.post("/api/encounters",
                      json={"patient_id": patient["id"], "org_id": a["id"], "encounter_type": "outpatient"},
                      headers=doc_a)
    assert enc.status_code == 201, enc.text

    seq = itertools.count(1)

    def new_consult() -> int:
        # 同一患者同一病种只能开一条在进行的会话（部分唯一索引），每条用各自的病种码
        db = SessionLocal()
        try:
            row = SpdConsult(patient_id=patient["id"], program_code=f"spdc{next(seq)}", status="open")
            db.add(row)
            db.commit()
            return row.id
        finally:
            db.close()

    return {"doc_a": doc_a, "doc_b": doc_b, "admin": admin, "patient_id": patient["id"],
            "new_consult": new_consult}


def _consult_logs(patient_id: int) -> int:
    db = SessionLocal()
    try:
        return db.query(AccessLog).filter(
            AccessLog.patient_id == patient_id, AccessLog.resource == "spd_consult"
        ).count()
    finally:
        db.close()


def _status(consult_id: int) -> str:
    db = SessionLocal()
    try:
        return db.get(SpdConsult, consult_id).status
    finally:
        db.close()


def test_无关机构不能关掉别家患者的咨询(client, consult_world):
    cid = consult_world["new_consult"]()
    r = client.post(f"/api/spd/consults/{cid}/close", headers=consult_world["doc_b"])
    assert r.status_code == 403, r.text
    assert _status(cid) == "open"


def test_有业务关系的照常能关且留痕(client, consult_world):
    cid = consult_world["new_consult"]()
    before = _consult_logs(consult_world["patient_id"])
    r = client.post(f"/api/spd/consults/{cid}/close", headers=consult_world["doc_a"])
    assert r.status_code == 200, r.text
    assert _status(cid) == "closed"
    assert _consult_logs(consult_world["patient_id"]) == before + 1


def test_无关机构不能给别家患者派随访(client, consult_world):
    cid = consult_world["new_consult"]()
    r = client.post(f"/api/spd/consults/{cid}/to-followup", json={}, headers=consult_world["doc_b"])
    assert r.status_code == 403, r.text


def test_有业务关系的照常能转随访(client, consult_world):
    cid = consult_world["new_consult"]()
    r = client.post(f"/api/spd/consults/{cid}/to-followup", json={}, headers=consult_world["doc_a"])
    assert r.status_code in (200, 201), r.text


def test_咨询清单按可见患者收口(client, consult_world):
    cid = consult_world["new_consult"]()

    def ids(headers):
        r = client.get("/api/spd/consults?limit=500", headers=headers)
        assert r.status_code == 200, r.text
        return {row["id"] for row in r.json()}

    assert cid not in ids(consult_world["doc_b"]), "乙院看得见甲院患者的咨询（带姓名与病种）"
    assert cid in ids(consult_world["doc_a"])
    assert cid in ids(consult_world["admin"])


def test_会话号不存在照旧404(client, consult_world):
    for path in ("close", "to-followup"):
        r = client.post(f"/api/spd/consults/987654/{path}", json={}, headers=consult_world["doc_b"])
        assert r.status_code == 404, (path, r.status_code, r.text)
