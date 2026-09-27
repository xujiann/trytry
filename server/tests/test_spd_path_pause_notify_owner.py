"""路径因进入条件暂停，只通知主管医生：档案没配主管医生就一条都不发，路径负责人也不知道（P2-503，第九批「通知承诺」扫描 W2-11）。

`service.advance_path` 的注释写着「不满足时**暂停并通知**，不静默跳过——静默跳过的表现是『路径停在那里且没人知道为什么』」；
可通知只发给 `enrollment.doctor_user_id`，没配主管医生的档案（纳管时这一项可空）暂停了一条消息都没有。路径实例自己有负责人
（`owner_user_id`，启动路径的人，路径页可改），从不通知他。

修法：暂停时通知主管医生与路径负责人，同一个人只发一条。
"""
import pytest

B = "/api/spd"
PROGRAM = "p2503_htn"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2503 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    prog = client.post(f"{B}/programs", headers=admin, json={"code": PROGRAM, "name": "P2503 高血压", "category": "chronic"})
    assert prog.status_code == 201, prog.text
    template = client.post(f"{B}/path-templates", headers=admin, json={
        "program_id": prog.json()["id"], "code": "P2503-PATH", "name": "P2503 条件路径"}).json()["id"]
    for node in ({"key": "p2503_n1", "name": "首次随访", "seq": 1, "due_days": 7},
                 {"key": "p2503_n2", "name": "风险复评", "seq": 2, "due_days": 5,
                  "enter_condition": [{"field": "risk_level", "op": "==", "value": "high"}]}):
        assert client.post(f"{B}/path-templates/{template}/nodes", headers=admin, json=node).status_code == 201
    assert client.post(f"{B}/path-templates/{template}/status", headers=admin,
                       json={"status": "published"}).status_code == 200
    doctor = client.post("/api/users", headers=admin, json={
        "username": "p2503_doc", "password": "passw0rd1", "full_name": "P2503 主管医生", "role": "doctor", "org_id": org})
    assert doctor.status_code in (200, 201), doctor.text
    from app.database import SessionLocal
    from app.models import User

    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
    return {"org": org, "template": template, "doctor": doctor.json()["id"], "admin": admin_id, "n": 0}


def _paused(client, admin, world, doctor_user_id):
    """建一份档案（可带主管医生）、由管理员启动路径（负责人即管理员），办完首节点 → 进入条件不满足而暂停。"""
    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2503 患者{world['n']}", "id_card": f"33028119750303{world['n']:03d}X"}).json()["id"]
    body = {"patient_id": patient, "program_code": PROGRAM, "org_id": world["org"]}
    if doctor_user_id:
        body["doctor_user_id"] = doctor_user_id
    enrollment = client.post(f"{B}/enrollments", headers=admin, json=body)
    assert enrollment.status_code == 201, enrollment.text
    instance = client.post(f"{B}/path-instances", headers=admin, json={
        "enrollment_id": enrollment.json()["id"], "template_id": world["template"]})
    assert instance.status_code == 201, instance.text
    iid = instance.json()["id"]
    first = client.get(f"{B}/path-instances/{iid}", headers=admin).json()["nodes"][0]["tasks"][0]["id"]
    done = client.post(f"{B}/tasks/{first}/complete", headers=admin, json={})
    assert done.status_code == 200 and done.json()["advanced"]["status"] == "paused", done.text
    return iid


def _recipients(instance_id):
    from app.database import SessionLocal
    from app.models import Notification

    with SessionLocal() as db:
        return sorted(n.user_id for n in db.query(Notification).filter(
            Notification.link_type == "spd_path_instance", Notification.link_id == instance_id,
            Notification.title == "专病路径已暂停"))


def test_没配主管医生_通知路径负责人(client, admin, world):
    assert _recipients(_paused(client, admin, world, None)) == [world["admin"]]   # 修前 []：暂停了没人知道


def test_主管医生与路径负责人都通知(client, admin, world):
    assert _recipients(_paused(client, admin, world, world["doctor"])) == sorted([world["admin"], world["doctor"]])
