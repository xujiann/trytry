"""出生证明 / 缺陷登记填了不存在的患者编号：原先回 503「编号分配连续冲突，请稍后重试」，怎么重试都是这一句（P2-396）。

签发接口只在死亡证明那一支查患者存在；出生 / 缺陷两类的患者编号可空，填了却不查，写库撞外键——而这张表的
编号是服务端取号，`insert_with_retry` 把任何 `IntegrityError` 都当成「号撞了」，重算序号连试 12 次，最后回 503。
开票人看到的是「稍后重试」，重试多少次都一样。修后三类证明填了患者编号一律先查，不存在 404、一行不写。
"""
import pytest

from app.database import SessionLocal
from app.models import MedicalCert

MISSING = 987654321


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P2396 证明签发医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]


def _certs():
    with SessionLocal() as db:
        return db.query(MedicalCert).count()


@pytest.mark.parametrize("cert_type,detail", [("birth", ""), ("defect", "先天性心脏病"), ("death", "心肌梗死")])
def test_填了不存在的患者编号_404不是503_一行不写(client, admin, org, cert_type, detail):
    before = _certs()
    got = client.post("/api/certs", headers=admin, json={
        "cert_type": cert_type, "name": "P2396 甲", "event_date": "2026-09-01", "org_id": org,
        "detail": detail, "patient_id": MISSING})
    assert (got.status_code, got.json()["detail"]) == (404, "患者不存在"), got.text   # 修前出生 / 缺陷 503
    assert _certs() == before


def test_填了存在的患者编号或不填_照常签发(client, admin, org):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2396 新生儿", "id_card": "330127202609012396"}).json()["id"]
    linked = client.post("/api/certs", headers=admin, json={
        "cert_type": "birth", "name": "P2396 新生儿", "event_date": "2026-09-01", "org_id": org, "patient_id": patient})
    assert linked.status_code == 201, linked.text
    bare = client.post("/api/certs", headers=admin, json={
        "cert_type": "birth", "name": "P2396 乙", "event_date": "2026-09-01", "org_id": org})
    assert bare.status_code == 201, bare.text
    assert client.post("/api/certs", headers=admin, json={
        "cert_type": "death", "name": "P2396 丙", "event_date": "2026-09-01", "org_id": org,
        "detail": "心肌梗死"}).json()["detail"] == "死亡医学证明须关联患者档案"   # 死亡那一支照旧先要求关联
