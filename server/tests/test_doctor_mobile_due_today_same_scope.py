"""医生移动端「我的待办 N 条（今日到期 M）」的 N 与 M 同一个范围（P2-550，第十批「同源数字承诺」扫描 X2-6）。

N 取 `todo.open`（本人、限可见机构），M 却取 `calendar.tasks`（本人名下今天到期的、不限机构）：一位医生名下挂着
别家机构派给他的任务时，「今日到期」可以比「我的待办」还多。修后 M 取同一块统计里的 `todo.due_today`。
"""
from pathlib import Path

import pytest

DOCTOR_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "m" / "doctor.js"


@pytest.fixture(scope="module")
def world(client, admin):
    from app import clock
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    own, other = (client.post("/api/organizations", headers=admin, json={
        "name": f"P2550 {name}", "org_type": "township", "level": "township"}).json()["id"] for name in ("本院", "别家"))
    doctor = client.post("/api/users", headers=admin, json={
        "username": "p2550_doc", "password": "pass123456", "role": "doctor", "org_id": own})
    assert doctor.status_code == 201, doctor.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2550 患者", "id_card": "330106197205052550"}).json()["id"]
    with SessionLocal() as db:
        for org in (own, other):
            db.add(SpdTask(patient_id=patient, org_id=org, task_type="followup", title=f"P2550 {org}",
                           status="claimed", due_date=clock.today().isoformat(), assignee_id=doctor.json()["id"]))
        db.commit()
    token = client.post("/api/auth/login", json={"username": "p2550_doc", "password": "pass123456"}).json()
    return {"Authorization": f"Bearer {token['access_token']}"}


def test_今日到期与我的待办同一个范围(client, world):
    wb = client.get("/api/spd/workbench/doctor-mobile", headers=world).json()
    # 两个数本来就不一样：日程按人（含别家派的），待办按本人可见机构
    assert (wb["todo"]["open"], wb["todo"]["due_today"], wb["calendar"]["tasks"]) == (1, 1, 2)
    line = next(l for l in DOCTOR_JS.read_text(encoding="utf-8").splitlines() if "我的待办" in l)
    assert "wb.todo.open" in line and "wb.todo.due_today" in line, line   # 修前配的是 wb.calendar.tasks
    assert "wb.calendar.tasks" not in line, line
