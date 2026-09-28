"""手工建任务显式带纳管档案号，档案得在管（第十五批「停用对象仍被引用」扫描 S1-3）。

同一个处理函数两种口径：选了病种、没填档案号的只挂在管的档案（P1-139 同一族）；显式带档案号的原先只查在不在——
登记死亡 / 排除 / 迁出的档案照挂新任务（201、派给原主管医生、待接收），结案收尾已经跑过，之后没人收。
改档、绑服务包、启动路径对非在管档案一律 409（P2-226 收了照护一族五个入口，这一处是跨模块的 `spawn_task` 建的子行，
「父对象结束」闸门 P1-104 看不见）。修后同一句 409，一行不写；在管的照常。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "S1-3 卫生院", "org_type": "township", "level": "township"}).json()["id"]

    def enrolled(n):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"S1-3 患者{n}", "id_card": f"33019219600101{1300 + n:04d}"}).json()["id"]
        resp = client.post(f"{B}/enrollments", headers=admin, json={
            "patient_id": patient, "program_code": "hypertension", "org_id": org})
        assert resp.status_code == 201, resp.text
        return patient, resp.json()["id"]

    dead, alive = enrolled(1), enrolled(2)
    resp = client.post(f"{B}/enrollments/{dead[1]}/lifecycle", headers=admin, json={"event": "death", "reason": "病故"})
    assert resp.status_code == 200, resp.text
    return {"org": org, "dead": dead, "alive": alive}


def _tasks(patient_id):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        return db.query(SpdTask).filter(SpdTask.patient_id == patient_id, SpdTask.source == "manual").count()


def test_死亡档案显式带号建任务_409一行不写(client, admin, world):
    patient, enrollment = world["dead"]
    resp = client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": patient, "enrollment_id": enrollment, "title": "S1-3 随访", "org_id": world["org"]})
    assert resp.status_code == 409, resp.text                                   # 修前 201
    assert resp.json() == {"detail": "非在管状态的档案不可新建任务，请先恢复管理"}
    assert _tasks(patient) == 0


def test_在管档案照常(client, admin, world):
    patient, enrollment = world["alive"]
    resp = client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": patient, "enrollment_id": enrollment, "title": "S1-3 随访", "org_id": world["org"]})
    assert resp.status_code == 201 and resp.json()["enrollment_id"] == enrollment, resp.text
