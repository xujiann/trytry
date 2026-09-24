"""门急诊按就诊号读处置记录与护理记录的归属校验与留痕（P0-22）。

`GET /api/outpatient/encounters/{id}/treatments` 与 `…/nursing-records` 原先连调用方身份都
不收：乙院医生（与该患者毫无业务关系）按就诊号就能读甲院的处置（换药、雾化、剂量、不良反应）
与护理记录——2026-09-24 实测 200。

同一个文件里，按患者查处置史（`/treatments?patient_id=`）与就诊文书完整性自查（`/completeness`）
早就按患者可见性判定并留痕（后者的注释写着"完整性清单会暴露该次就诊有哪些文书，按患者维度守"）——
清单都守了，清单背后的内容本身反而没守。口径在同一个文件里是现成的，照抄。

两个方向都钉：无关机构 403；有业务关系的照常 200 并留下 `AccessLog`；就诊号不存在照旧回空清单。
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
def outpatient_world(client):
    """甲院一次门诊就诊，记了一条处置、一条护理；乙院一名医师与该患者毫无关系。"""
    admin = _login(client, "admin", "admin123")
    a = client.post("/api/organizations",
                    json={"name": "门诊读甲院", "org_type": "lead_hospital", "level": "county"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "门诊读乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    for uname, org in (("opr_doc_a", a), ("opr_doc_b", b)):
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": "doctor", "org_id": org["id"]},
                    headers=admin)
    doc_a, doc_b = _login(client, "opr_doc_a"), _login(client, "opr_doc_b")
    patient = client.post("/api/patients",
                          json={"name": "门诊读患者", "id_card": "320000199505055671"},
                          headers=admin).json()
    enc = client.post("/api/encounters",
                      json={"patient_id": patient["id"], "org_id": a["id"], "encounter_type": "outpatient"},
                      headers=doc_a)
    assert enc.status_code == 201, enc.text
    eid = enc.json()["id"]
    t = client.post(f"/api/outpatient/encounters/{eid}/treatments",
                    json={"treatment_name": "伤口换药", "site": "左小腿", "reaction": "无不适"},
                    headers=doc_a)
    assert t.status_code == 201, t.text
    n = client.post(f"/api/outpatient/encounters/{eid}/nursing-records",
                    json={"content": "观察 30 分钟无不良反应", "nurse_name": "甲护士"},
                    headers=doc_a)
    assert n.status_code == 201, n.text
    return {"doc_a": doc_a, "doc_b": doc_b, "patient_id": patient["id"], "encounter_id": eid}


READS = {"treatments": "treatment", "nursing-records": "outpatient_nursing"}


@pytest.mark.parametrize("path", sorted(READS))
def test_无关机构按就诊号读处置与护理403(client, outpatient_world, path):
    r = client.get(f"/api/outpatient/encounters/{outpatient_world['encounter_id']}/{path}",
                   headers=outpatient_world["doc_b"])
    assert r.status_code == 403, (path, r.status_code, r.text)


@pytest.mark.parametrize("path", sorted(READS))
def test_有业务关系的照常能读且留痕(client, outpatient_world, path):
    def count():
        db = SessionLocal()
        try:
            return db.query(AccessLog).filter(
                AccessLog.patient_id == outpatient_world["patient_id"], AccessLog.resource == READS[path]
            ).count()
        finally:
            db.close()

    before = count()
    r = client.get(f"/api/outpatient/encounters/{outpatient_world['encounter_id']}/{path}",
                   headers=outpatient_world["doc_a"])
    assert r.status_code == 200, (path, r.text)
    assert r.json(), path
    assert count() == before + 1, f"{path} 放行了却没留痕"


@pytest.mark.parametrize("path", sorted(READS))
def test_就诊号不存在照旧回空清单(client, outpatient_world, path):
    """原先就是 200 + 空清单；补归属校验不改这个响应（空清单什么也不泄露）。"""
    r = client.get(f"/api/outpatient/encounters/987654/{path}", headers=outpatient_world["doc_b"])
    assert r.status_code == 200 and r.json() == [], (path, r.status_code, r.text)
