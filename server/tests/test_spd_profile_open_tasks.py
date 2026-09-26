"""专病 360 档案卡片上的「待办 N」与工作台、日报同一口径，且数全部任务（P2-138）。

卡片的待办数原先在**最近 20 条**任务里按手写的 pending / claimed / doing / overdue 数：
- 漏了待审核（submitted）与退回（rejected）——P1-127 起它们都是「未结束」，工作台、日报都算；
- 一位随访多年的患者任务一多，更早的那条待办被新任务挤出前 20 条，卡片就说「待办 0」。

修法：按 `service.TASK_OPEN_STATUSES` 在库里数这份档案的全部任务；「最近任务」照旧给最新 10 条。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment, SpdTask

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2138 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2138 患者", "id_card": "330106196707071380", "gender": "男"}).json()["id"]
    with SessionLocal() as db:
        enrollment = SpdEnrollment(patient_id=patient, program_code="P2138_HTN", org_id=org, status="active")
        db.add(enrollment)
        db.flush()

        def task(title, status):
            return SpdTask(patient_id=patient, enrollment_id=enrollment.id, program_code="P2138_HTN",
                           org_id=org, title=title, status=status, task_type="followup")

        # 由旧到新：一条早年的待办，25 条办结的，再一条待审核、一条退回的
        db.add(task("早年派下的随访", "pending"))
        db.flush()
        db.add_all([task(f"办结的随访{i}", "done") for i in range(25)])
        db.flush()
        db.add(task("交上来待审核的", "submitted"))
        db.flush()
        db.add(task("审核退回的", "rejected"))
        db.commit()
    return {"patient": patient}


def test_待办数含待审核与退回_也不止看最近20条(client, admin, world):
    body = client.get(f"{B}/patients/{world['patient']}/profile", headers=admin)
    assert body.status_code == 200, body.text
    [card] = body.json()["programs"]
    assert card["open_tasks"] == 3   # 修前 0：前 20 条里没有手写那四个状态的，早年那条待办又被挤出去了
    assert [t["title"] for t in card["recent_tasks"]] == (
        ["审核退回的", "交上来待审核的"] + [f"办结的随访{i}" for i in range(24, 16, -1)])
