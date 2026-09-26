"""慢专病路径实例的两条读接口按 id 直取、不看归属也不留痕（P0-44）。

`GET /api/spd/path-instances/{id}`（执行明细）回患者姓名、病种、当前阶段与每个节点的任务；
`GET /api/spd/path-nodes/{id}/enter-check?instance_id=` 拿节点的进入条件对实例所属患者求值。两条原先都不收调用方：
一家与患者毫无关系的卫生院的医生，按实例号就翻得到别家纳管的患者（修前实测 200），还能拿进入条件试出患者的风险分层。
清单（`GET /path-instances`）只列本机构纳管的，纳管档案详情（`GET /enrollments/{id}`）按患者可见性判并留痕——
同一份东西，明细什么都不看。两条原先登记在读侧欠账名单里（`test_stage15_horizontal.NEWLY_VISIBLE_UNGUARDED_READS`、
`test_unscopable_patient_reads.ONEHOP_UNSCOPABLE_READS`），同文件兄弟端点的口径现成，没有口径问题。

修法：与纳管档案详情同一句——按实例所属纳管档案的患者 `assert_patient_visible`，留痕资源记「慢专病临床路径」。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"


def _login(client, username, password="pw123456"):
    token = client.post("/api/auth/login", json={"username": username, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    from app.spd.models import SpdPathNode, SpdPathTemplate, SpdProgram

    a = client.post("/api/organizations", headers=admin, json={
        "name": "P0044 纳管卫生院", "org_type": "township", "level": "township"}).json()["id"]
    b = client.post("/api/organizations", headers=admin, json={
        "name": "P0044 无关卫生院", "org_type": "township", "level": "township"}).json()["id"]
    for username, org in (("p0044_doc_a", a), ("p0044_doc_b", b)):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pw123456", "full_name": username, "role": "doctor", "org_id": org})
        assert created.status_code == 201, created.text
    with SessionLocal() as db:
        program = db.query(SpdProgram).filter_by(code="hypertension").one()
        template = SpdPathTemplate(program_id=program.id, code="P0044_PATH", name="P0044 两节点路径",
                                   status="published")
        db.add(template)
        db.flush()
        first = SpdPathNode(template_id=template.id, key="n1", name="首诊", seq=1)
        second = SpdPathNode(template_id=template.id, key="n2", name="强化管理", seq=2,
                             enter_condition=[{"field": "risk_level", "op": "==", "value": "high"}])
        db.add_all([first, second])
        db.commit()
        template_id, node_id = template.id, second.id
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P0044 患者", "id_card": "330127197309090044"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": a})
    assert enrollment.status_code == 201, enrollment.text
    started = client.post(f"{B}/path-instances", headers=admin, json={
        "enrollment_id": enrollment.json()["id"], "template_id": template_id})
    assert started.status_code == 201, started.text
    return {"patient": patient, "instance": started.json()["id"], "node": node_id,
            "doc_a": _login(client, "p0044_doc_a"), "doc_b": _login(client, "p0044_doc_b")}


def _access_rows(patient_id, resource="spd_path"):
    from app.models import AccessLog

    with SessionLocal() as db:
        return db.query(AccessLog).filter_by(patient_id=patient_id, resource=resource).count()


def test_无关机构按实例号读执行明细_403(client, world):
    got = client.get(f"{B}/path-instances/{world['instance']}", headers=world["doc_b"])
    assert got.status_code == 403, got.text   # 修前 200，回患者姓名与病种
    assert "P0044 患者" not in got.text


def test_无关机构拿进入条件对别家患者求值_403(client, world):
    got = client.get(f"{B}/path-nodes/{world['node']}/enter-check",
                     params={"instance_id": world["instance"]}, headers=world["doc_b"])
    assert got.status_code == 403, got.text   # 修前 200：allowed 就是患者风险分层的答案


def test_纳管机构照常看且每次留痕(client, world):
    before = _access_rows(world["patient"])
    detail = client.get(f"{B}/path-instances/{world['instance']}", headers=world["doc_a"])
    assert detail.status_code == 200, detail.text
    assert detail.json()["patient_name"] == "P0044 患者"
    check = client.get(f"{B}/path-nodes/{world['node']}/enter-check",
                       params={"instance_id": world["instance"]}, headers=world["doc_a"])
    assert check.status_code == 200, check.text
    assert _access_rows(world["patient"]) == before + 2   # 修前 0：两条读接口都不留痕


def test_留痕资源有中文名():
    from app.routers.access_logs import resource_name

    assert resource_name("spd_path") == "慢专病临床路径"


def test_不存在的实例仍是404_不因归属判定变成403(client, world):
    assert client.get(f"{B}/path-instances/999999", headers=world["doc_b"]).status_code == 404
    assert client.get(f"{B}/path-nodes/{world['node']}/enter-check",
                      params={"instance_id": 999999}, headers=world["doc_b"]).status_code == 404
