"""启动患者路径不看路径模板是不是这个病种的：高血压患者能跑上糖尿病的路径（P2-95）。

路径模板挂在病种上（`program_id`），纳管档案也挂在病种上（`program_code`）；启动路径（`POST /path-instances`）只查档案
在管、模板已发布且有节点，两边的病种对不对得上一眼不看。管理端「启动患者路径」的模板下拉又列的是**全部病种**已发布的
模板、不带病种名——填一个高血压档案号、下拉里点到糖尿病的路径，201：首节点任务派下去，任务挂在高血压档案上、内容是
糖尿病路径的节点；实例的「当前阶段」取自糖尿病的阶段定义，按阶段的统计与高血压的阶段对不上；节点的进入条件拿高血压
档案的事实去判糖尿病的条件。与 P2-89（手工建任务挂的档案病种与任务不一致 422）同一个口径。

修法：启动时模板的病种须是档案的病种，不一致 422，什么都不建。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdPathNode, SpdPathTemplate, SpdProgram

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P295 路径卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P295 患者", "id_card": "330127196612120295"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org})
    assert enrollment.status_code == 201, enrollment.text
    templates = {}
    with SessionLocal() as db:
        for program_code in ("hypertension", "diabetes"):
            program = db.query(SpdProgram).filter_by(code=program_code).one()
            template = SpdPathTemplate(program_id=program.id, code=f"P295_{program_code}",
                                       name=f"P295 {program_code} 路径", status="published")
            db.add(template)
            db.flush()
            db.add(SpdPathNode(template_id=template.id, key="n1", name="首诊", seq=1))
            templates[program_code] = template.id
        db.commit()
    return {"enrollment": enrollment.json()["id"], "templates": templates}


def _instances(enrollment_id):
    from app.database import SessionLocal
    from app.spd.models import SpdPathInstance

    with SessionLocal() as db:
        return db.query(SpdPathInstance).filter_by(enrollment_id=enrollment_id).count()


def test_别的病种的路径模板_启动422_什么都不建(client, admin, world):
    resp = client.post(f"{B}/path-instances", headers=admin, json={
        "enrollment_id": world["enrollment"], "template_id": world["templates"]["diabetes"]})
    assert resp.status_code == 422, resp.text   # 修前 201
    assert resp.json() == {"detail": "路径模板的病种与纳管档案不一致"}
    assert _instances(world["enrollment"]) == 0


def test_本病种的路径模板照常启动(client, admin, world):
    resp = client.post(f"{B}/path-instances", headers=admin, json={
        "enrollment_id": world["enrollment"], "template_id": world["templates"]["hypertension"]})
    assert resp.status_code == 201, resp.text
    assert _instances(world["enrollment"]) == 1
