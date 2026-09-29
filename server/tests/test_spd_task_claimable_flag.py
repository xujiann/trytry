"""任务中心不再给别人名下的任务摆「接收」：清单行带 `claimable`，与接收接口同一判据现算（P2-799，第二十二批「人员与机构
变动之后」扫描 X3-4 的页面一侧；接手的工作记给谁属 P1-206）。

接收接口只收「待接收 / 已超期、空着或本人名下」的任务（`claim_task`：静默改责任人会让原责任人白干一场），还要能以任务
机构的名义写入（`_load_task`）。任务中心的「接收」原先只看状态：主管医生派生的任务在同事那里每一条都摆着「接收」，
点了必 409「该任务已由其他人员接收」；看得见、写不了的别家任务点了 403。修后按清单行的 `claimable` 摆。
"""
from pathlib import Path

import pytest
from conftest import login

B = "/api/spd"
PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.models import Encounter
    from app.spd.models import SpdTask

    county = client.post("/api/organizations", headers=admin, json={
        "name": "P2799 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    town = client.post("/api/organizations", headers=admin, json={
        "name": "P2799 卫生院", "org_type": "township", "level": "township", "parent_id": county}).json()["id"]
    users = {}
    for name, org in (("a", town), ("b", town), ("county", county)):
        resp = client.post("/api/users", headers=admin, json={
            "username": f"p2799_{name}", "password": "pass123456", "role": "doctor", "org_id": org})
        assert resp.status_code == 201, resp.text
        users[name] = resp.json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2799 患者", "id_card": "330106197307072799"}).json()["id"]
    tasks = {}
    with SessionLocal() as db:
        # 县医院接诊过这位患者：按患者查任务清单时看得见卫生院的任务（患者可见即不按机构筛），写却只能写本院
        db.add(Encounter(patient_id=patient, org_id=county, encounter_type="outpatient", diagnosis_name="高血压"))
        for key, status, assignee in (("mine", "pending", users["a"]), ("theirs", "pending", users["b"]),
                                      ("free", "pending", None), ("free_overdue", "overdue", None),
                                      ("theirs_overdue", "overdue", users["b"]), ("claimed_mine", "claimed", users["a"]),
                                      ("done", "done", None)):
            task = SpdTask(patient_id=patient, org_id=town, program_code="hypertension", task_type="followup",
                           title=f"P2799 {key}", status=status, assignee_id=assignee, due_date="2026-12-01")
            db.add(task)
            db.flush()
            tasks[key] = task.id
        db.commit()
    heads = {name: login(client, f"p2799_{name}", "pass123456") for name in users}
    return {"users": users, "tasks": tasks, "heads": heads, "patient": patient}


def _claimable(client, headers, patient):
    rows = client.get(f"{B}/tasks", headers=headers, params={"patient_id": patient, "limit": 50})
    assert rows.status_code == 200, rows.text
    return {r["id"]: r["claimable"] for r in rows.json()}


def test_别人名下的不能接收_空着的与本人名下的能(client, world):
    tasks, got = world["tasks"], _claimable(client, world["heads"]["a"], world["patient"])
    assert got == {tasks["mine"]: True, tasks["theirs"]: False, tasks["free"]: True, tasks["free_overdue"]: True,
                   tasks["theirs_overdue"]: False, tasks["claimed_mine"]: False, tasks["done"]: False}   # 修前没有这个键
    # 与接口同口径：标 False 的点了 409，标 True 的点了成
    for key in ("theirs", "theirs_overdue", "claimed_mine", "done"):
        resp = client.post(f"{B}/tasks/{tasks[key]}/claim", headers=world["heads"]["a"])
        assert resp.status_code == 409, (key, resp.text)
    resp = client.post(f"{B}/tasks/{tasks['free_overdue']}/claim", headers=world["heads"]["a"])
    assert resp.status_code == 200 and resp.json()["assignee_id"] == world["users"]["a"], resp.text


def test_看得见写不了的别家任务不能接收(client, world):
    """县医院接诊过这位患者，按患者查看得见卫生院的任务；接收却要以任务机构的名义写入——点了 403。"""
    tasks = world["tasks"]
    got = _claimable(client, world["heads"]["county"], world["patient"])
    assert got.get(tasks["free"]) is False, got
    resp = client.post(f"{B}/tasks/{tasks['free']}/claim", headers=world["heads"]["county"])
    assert resp.status_code == 403, resp.text


def test_任务中心按标记摆接收():
    start = PAGE.index("function spdTaskActions(t) {")
    body = PAGE[start:PAGE.index("\n}\n", start)]
    assert 'if (t.claimable) parts.push(b("data-task-claim", "接收"));' in body
    assert 't.status === "pending" || t.status === "overdue"' not in body   # 修前只看状态
