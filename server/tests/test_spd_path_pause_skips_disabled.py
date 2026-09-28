"""路径因进入条件暂停，通知不发给停用的账号（第十六批「通知收件人」扫描 T2-2）。

P2-503 把暂停通知发给主管医生与路径负责人；两人任一停用了照发——消息落进一个登不上的收件箱，在岗的人照样不知道路径
停了。派任务早已按 `usable_or_none` 跳过停用的主管医生（P1-205），这里同一口径：停用的不发，在用的照发。
两人都停用时一个都不剩，该回落给谁另行裁定（见待裁定清单）。
"""
import pytest

B = "/api/spd"
PROGRAM = "t22_htn"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "T2-2 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    prog = client.post(f"{B}/programs", headers=admin, json={"code": PROGRAM, "name": "T2-2 高血压", "category": "chronic"})
    assert prog.status_code == 201, prog.text
    template = client.post(f"{B}/path-templates", headers=admin, json={
        "program_id": prog.json()["id"], "code": "T22-PATH", "name": "T2-2 条件路径"}).json()["id"]
    for node in ({"key": "t22_n1", "name": "首次随访", "seq": 1, "due_days": 7},
                 {"key": "t22_n2", "name": "风险复评", "seq": 2, "due_days": 5,
                  "enter_condition": [{"field": "risk_level", "op": "==", "value": "high"}]}):
        assert client.post(f"{B}/path-templates/{template}/nodes", headers=admin, json=node).status_code == 201
    assert client.post(f"{B}/path-templates/{template}/status", headers=admin,
                       json={"status": "published"}).status_code == 200
    doctor = client.post("/api/users", headers=admin, json={
        "username": "t22_doc", "password": "passw0rd1", "full_name": "T2-2 主管医生", "role": "doctor", "org_id": org})
    assert doctor.status_code in (200, 201), doctor.text
    from app.database import SessionLocal
    from app.models import User

    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
    return {"org": org, "template": template, "doctor": doctor.json()["id"], "admin": admin_id}


def _recipients(instance_id):
    from app.database import SessionLocal
    from app.models import Notification

    with SessionLocal() as db:
        return sorted(n.user_id for n in db.query(Notification).filter(
            Notification.link_type == "spd_path_instance", Notification.link_id == instance_id,
            Notification.title == "专病路径已暂停"))


def test_主管医生停用后路径暂停_只通知在用的路径负责人(client, admin, world):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "T2-2 患者", "id_card": "330281197503032211"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": PROGRAM, "org_id": world["org"], "doctor_user_id": world["doctor"]})
    assert enrollment.status_code == 201, enrollment.text
    instance = client.post(f"{B}/path-instances", headers=admin, json={
        "enrollment_id": enrollment.json()["id"], "template_id": world["template"]})   # 负责人即启动的管理员
    assert instance.status_code == 201, instance.text
    iid = instance.json()["id"]
    first = client.get(f"{B}/path-instances/{iid}", headers=admin).json()["nodes"][0]["tasks"][0]["id"]
    disabled = client.patch(f"/api/users/{world['doctor']}/status", json={"status": "disabled"}, headers=admin)
    assert disabled.status_code == 200, disabled.text

    done = client.post(f"{B}/tasks/{first}/complete", headers=admin, json={})
    assert done.status_code == 200 and done.json()["advanced"]["status"] == "paused", done.text
    assert _recipients(iid) == [world["admin"]]   # 修前还有停用的主管医生
