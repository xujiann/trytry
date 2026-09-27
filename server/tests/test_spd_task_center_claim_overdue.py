"""任务中心给已超期的任务画「接收」（P2-600，第十二批「按钮 vs 状态机」扫描 Z2-10）。

接收接口收待接收与已超期两种（`service.TASK_CLAIMABLE_STATUSES`），医生手机端的待办卡片早就两种都给（P2-84），中心
工作台的「无人认领」也把没人接的超期任务算进去（P2-245）；管理端任务中心的动作按钮（`spdTaskActions`）却只给待接收
的画「接收」——没人接、已超期的任务在这一页上只能分派，接不了。
"""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")
MOBILE = (Path(__file__).resolve().parents[1] / "app" / "static" / "m" / "doctor.js").read_text(encoding="utf-8")


def _world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2600 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2600 患者", "id_card": "330106197206062600"}).json()["id"]
    return org, patient


def test_已超期的任务接口照收接收(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    org, patient = _world(client, admin)
    with SessionLocal() as db:
        task = SpdTask(patient_id=patient, org_id=org, program_code="hypertension", task_type="followup",
                       title="P2600 超期随访", status="overdue", due_date="2020-01-01")
        db.add(task)
        db.commit()
        task_id = task.id
    claimed = client.post(f"/api/spd/tasks/{task_id}/claim", headers=admin)
    assert claimed.status_code == 200 and claimed.json()["status"] == "claimed", claimed.text


def test_任务中心与手机端同一口径给接收按钮():
    start = PAGE.index("function spdTaskActions(t) {")
    body = PAGE[start:PAGE.index("\n}\n", start)]
    assert 'if (t.status === "pending" || t.status === "overdue") parts.push(b("data-task-claim", "接收"));' in body
    assert '["pending", "overdue"].includes(t.status) ? b("data-spd-claim", "接收")' in MOBILE   # 手机端的口径
