"""启动路径进首节点不把节点阶段同步到纳管档案：实例上是「治疗期」，档案还是原来的阶段（P2-259）。

路径节点可以带阶段（`SpdPathNode.stage`）：推进到下一节点（`advance_path`）与恢复暂停的实例（`_resume_paused`）都把节点阶段
写回纳管档案（`enrollment.stage = node.stage`），按阶段取的管理目标、随访周期、测量值分级都读档案的阶段。启动（`start_path`）
进首节点时却只写了实例的当前阶段，档案不动。修法：启动与推进、恢复同一句。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdPathNode, SpdPathTemplate, SpdProgram

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2259 路径卫生院", "org_type": "township", "level": "township"}).json()["id"]
    with SessionLocal() as db:
        program = db.query(SpdProgram).filter_by(code="hypertension").one()
        staged = SpdPathTemplate(program_id=program.id, code="P2259_STAGED", name="P2259 带阶段路径", status="published")
        plain = SpdPathTemplate(program_id=program.id, code="P2259_PLAIN", name="P2259 不带阶段路径", status="published")
        db.add_all([staged, plain])
        db.flush()
        db.add_all([
            SpdPathNode(template_id=staged.id, key="n1", name="强化治疗", seq=1, stage="treatment"),
            SpdPathNode(template_id=staged.id, key="n2", name="稳定随访", seq=2, stage="stable"),
            SpdPathNode(template_id=plain.id, key="n1", name="首诊", seq=1),
        ])
        db.commit()
        return {"org": org, "staged": staged.id, "plain": plain.id, "n": 0}


def _enrollment(client, admin, world):
    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2259 患者{world['n']}", "id_card": f"33012719741010{world['n']:04d}"}).json()["id"]
    resp = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": world["org"]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _stage(enrollment_id):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment

    with SessionLocal() as db:
        return db.get(SpdEnrollment, enrollment_id).stage


def test_进首节点把节点阶段写回档案(client, admin, world):
    enrollment = _enrollment(client, admin, world)
    before = _stage(enrollment)
    assert before != "treatment"
    started = client.post(f"{B}/path-instances", headers=admin, json={
        "enrollment_id": enrollment, "template_id": world["staged"]})
    assert started.status_code == 201 and started.json()["current_stage"] == "treatment", started.text
    assert _stage(enrollment) == "treatment"   # 修前还是原来的阶段


def test_首节点不带阶段的_档案阶段照旧不动(client, admin, world):
    enrollment = _enrollment(client, admin, world)
    before = _stage(enrollment)
    started = client.post(f"{B}/path-instances", headers=admin, json={
        "enrollment_id": enrollment, "template_id": world["plain"]})
    assert started.status_code == 201, started.text
    assert _stage(enrollment) == before
