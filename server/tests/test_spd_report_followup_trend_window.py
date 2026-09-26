"""报告里的随访完成趋势不设上界：排在未来的随访以 0% 进图，本月完成率也被没到日子的拉低（P2-293）。

`_followup_trend`（报告段落「服务质量」「运行趋势」共用）取的是「近 30 天」——`planned_at >= 今天 - 30 天`，却没有
`<= 今天`。随访计划按方案一次排出多个时间点（出院后 7 / 30 / 90 / 180 天），排在未来的全是「未完成」：它们所在的
未来月份以 0% 进图，本月里还没到日子的也算进分母，完成率被拉低。

修法：窗口收到今天为止。
"""
from datetime import date

import pytest
from conftest import freeze_business_date

from app.database import SessionLocal


@pytest.fixture(scope="module")
def org(client, admin):
    from app.spd.models import SpdFollowupRecord

    org_id = client.post("/api/organizations", headers=admin, json={
        "name": "P2293 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2293 患者", "id_card": "330127197309092293"}).json()["id"]
    with SessionLocal() as db:
        for planned_at, status in [("2026-08-01", "done"),   # 30 天窗口之外
                                   ("2026-09-10", "done"), ("2026-09-20", "overdue"),
                                   ("2026-09-28", "planned"), ("2026-11-15", "planned")]:   # 后两条还没到日子
            db.add(SpdFollowupRecord(patient_id=patient, org_id=org_id, planned_at=planned_at, status=status))
        db.commit()
    return org_id


def test_趋势只到今天为止_未来的随访不进图(org):
    from app.spd.reporting import compose_section

    with freeze_business_date(date(2026, 9, 26)), SessionLocal() as db:
        out = compose_section(db, {"key": "trend", "title": "运行趋势"}, org, "monthly")
    assert out["series"] == [{"label": "2026-09", "total": 2, "done": 1, "rate": 50.0}]
    # 修前：[2026-09 共 3 条完成 1 条 33.3%，2026-11 共 1 条 0%]
