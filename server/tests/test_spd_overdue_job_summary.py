"""慢专病超期扫描的定时任务只报任务：同一趟标了超期的复诊、随访在调度日志里查不到（P2-377）。

`sweep_overdue` 一趟把任务、复诊、随访三类过了日期的都标成超期，返回四个数；定时任务
`spd_task_overdue_scan` 的处理数与摘要却只取任务那两个（「超期 N 条，其中升级 M 条」）。同批一并订正的是几处写错的
注释 / 示例（纳入规则「命中任一」其实是「全部满足」、问卷异常规则示例写成 `key`、「这两个任务」其实注册了四个）。

修法：处理数与摘要把复诊、随访也算上；推送频道仍只报任务数（它是任务超期的提醒）。
"""
from datetime import timedelta

from app.database import SessionLocal
from conftest import business_today


def test_超期扫描的摘要带上复诊与随访(client, admin):
    from app.spd.jobs import spd_task_overdue_scan
    from app.spd.models import SpdFollowupRecord, SpdRevisit

    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2377 患者", "id_card": "330102197707072377"}).json()["id"]
    yesterday = (business_today() - timedelta(days=2)).isoformat()
    with SessionLocal() as db:
        db.add(SpdRevisit(patient_id=patient, program_code="hypertension", plan_date=yesterday))
        db.add(SpdFollowupRecord(patient_id=patient, program_code="hypertension", scene="outpatient",
                                 planned_at=yesterday, status="planned", channel="phone"))
        db.commit()
    with SessionLocal() as db:
        count, summary = spd_task_overdue_scan(db)
        db.commit()
    assert "复诊超期" in summary and "随访超期" in summary, summary   # 修前「超期 N 条，其中升级 M 条」
    revisits = int(summary.split("复诊超期 ")[1].split(" 条")[0])
    followups = int(summary.split("随访超期 ")[1].split(" 条")[0])
    assert revisits >= 1 and followups >= 1 and count >= revisits + followups


def test_问卷异常规则注释示例用_field():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "spd" / "models.py").read_text(encoding="utf-8")
    assert '# [{"when":{"field":"pain"' in src and '"when":{"key":' not in src
