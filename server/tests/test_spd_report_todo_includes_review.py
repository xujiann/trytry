"""报告「待办任务」表把待审核的也列出来（P2-602，第十二批「计数 vs 清单」扫描 Z1-5）。

「总体概览」的「待办任务 N 条」按 `TASK_OPEN_STATUSES` 数（含待审核，与工作台同一口径），「待办任务」表只取在手的
（不含待审核）、「超期预警」表只取超期的——待审核的任务算进了 N、两张表都不列，概览说待办 5 条、两张表加起来 4 条。
"""
import pytest

PROGRAM = "P2602_PG"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2602 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2602 患者", "id_card": "330106197208082602"}).json()["id"]
    with SessionLocal() as db:
        for title, status, due in (("P2602 在手", "pending", "2099-01-01"), ("P2602 待审", "submitted", "2099-01-02"),
                                   ("P2602 超期", "overdue", "2020-01-01"), ("P2602 办结", "done", "2099-01-03")):
            db.add(SpdTask(patient_id=patient, org_id=org, program_code=PROGRAM, task_type="followup",
                           title=title, status=status, due_date=due))
        db.commit()
    return org


def test_概览的待办数等于两张表的行数_待审核的注明(world):
    from app.database import SessionLocal
    from app.spd.reporting import compose_section

    with SessionLocal() as db:
        summary = compose_section(db, {"key": "summary"}, world, "daily")
        todo = compose_section(db, {"key": "todo"}, world, "daily")["rows"]
        alert = compose_section(db, {"key": "alert"}, world, "daily")["rows"]
    assert summary["metrics"]["open_tasks"] == 3 == len(todo) + len(alert)   # 修前 todo 只有 1 行
    assert [r[0] for r in todo] == ["P2602 在手", "P2602 待审（待审核）"]
    assert [r[0] for r in alert] == ["P2602 超期"]
