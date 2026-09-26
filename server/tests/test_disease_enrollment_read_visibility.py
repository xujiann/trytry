"""专病入组明细按入组号直取、不收调用方也不留痕（P0-45）。

`GET /api/disease-programs/enrollments/{id}` 回患者进了哪个专病、在管还是出组、疗效与退出原因，以及每个路径节点
谁在哪天做了什么——原先连 `user` 形参都没有：一家与患者毫无关系的卫生院的医生按号就读得到（修前实测 200），不留痕。
同文件的清单带 `patient_id` 时按患者可见性判并留痕、不带时只列本机构的（`scope_patient_list`），写侧（记节点、出组）
按入组机构判——同一份东西，明细什么都不看。原先登记在读侧欠账名单里（`test_stage15_horizontal.
NEWLY_VISIBLE_UNGUARDED_READS`），兄弟端点的口径现成，没有口径问题。

修法：与清单带 `patient_id` 时同一句——按入组患者 `assert_patient_visible`，留痕资源同为「专病管理」。
"""
import pytest

from app.database import SessionLocal

B = "/api/disease-programs"


def _login(client, username, password="pw123456"):
    token = client.post("/api/auth/login", json={"username": username, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    from app.models import DiseaseEnrollment

    a = client.post("/api/organizations", headers=admin, json={
        "name": "P0045 入组医院", "org_type": "township", "level": "township"}).json()["id"]
    b = client.post("/api/organizations", headers=admin, json={
        "name": "P0045 无关卫生院", "org_type": "township", "level": "township"}).json()["id"]
    for username, org in (("p0045_doc_a", a), ("p0045_doc_b", b)):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pw123456", "full_name": username, "role": "doctor", "org_id": org})
        assert created.status_code == 201, created.text
    program = client.post(B, headers=admin, json={
        "code": "P0045_DP", "name": "P0045 肺癌专病", "path_nodes": [{"key": "dx", "name": "确诊"}]})
    assert program.status_code == 201, program.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P0045 患者", "id_card": "330127197309090045"}).json()["id"]
    with SessionLocal() as db:
        row = DiseaseEnrollment(program_id=program.json()["id"], patient_id=patient, org_id=a, status="enrolled",
                                enrolled_at="2026-09-01")
        db.add(row)
        db.commit()
        enrollment = row.id
    return {"patient": patient, "enrollment": enrollment,
            "doc_a": _login(client, "p0045_doc_a"), "doc_b": _login(client, "p0045_doc_b")}


def _access_rows(patient_id):
    from app.models import AccessLog

    with SessionLocal() as db:
        return db.query(AccessLog).filter_by(patient_id=patient_id, resource="disease_program").count()


def test_无关机构按入组号读明细_403(client, world):
    got = client.get(f"{B}/enrollments/{world['enrollment']}", headers=world["doc_b"])
    assert got.status_code == 403, got.text   # 修前 200：病种、在管状态、节点记录全在


def test_入组机构照常看且留痕(client, world):
    before = _access_rows(world["patient"])
    got = client.get(f"{B}/enrollments/{world['enrollment']}", headers=world["doc_a"])
    assert got.status_code == 200, got.text
    assert got.json()["patient_id"] == world["patient"]
    assert _access_rows(world["patient"]) == before + 1   # 修前不留痕


def test_不存在的入组仍是404(client, world):
    assert client.get(f"{B}/enrollments/999999", headers=world["doc_b"]).status_code == 404
