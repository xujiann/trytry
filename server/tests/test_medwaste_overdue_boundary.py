"""医废滞留按「收集后超过 2 天」判：收集满 2 天当天原先就报滞留（P2-134）。

预警清单、滞留扫描任务、驾驶舱指标三处原先各写 `collected_date <= 今天 − 2`：收集满 2 天的当天就进滞留预警——
预警回执自己算出的 `overdue_days` 是 0，页面却挂着「滞留」标签，与「收集超过 2 天仍未交接」（《医疗废物管理条例》
暂存不得超过 2 天）差一天。修法：三处共用 `medwaste.overdue_condition`，超过 2 天（第 3 天起）才算。
"""
from datetime import timedelta

import pytest

from app import clock


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P2134 卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _waste(client, admin, org, collected):
    resp = client.post("/api/medwaste", headers=admin, json={
        "org_id": org, "waste_type": "infectious", "weight_kg": 1.0, "collected_date": collected})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_满两天当天不报_第三天起报且超期天数从1起(client, admin, org):
    two_days = _waste(client, admin, org, "2026-09-24")
    three_days = _waste(client, admin, org, "2026-09-23")
    rows = {r["id"]: r for r in client.get("/api/medwaste/alerts?today=2026-09-26", headers=admin).json()}
    assert two_days not in rows   # 修前在：overdue_days 0 却挂「滞留」
    assert rows[three_days]["overdue_days"] == 1


def test_驾驶舱与扫描任务同一口径(client, admin, org):
    from app.database import SessionLocal
    from app.jobs import medwaste_overdue_scan
    from app.models import MedicalWaste
    from app.routers.metrics import q_medwaste_overdue

    today = clock.today()
    edge = _waste(client, admin, org, (today - timedelta(days=2)).isoformat())
    over = _waste(client, admin, org, (today - timedelta(days=3)).isoformat())
    with SessionLocal() as db:
        cockpit = {w.id for w in q_medwaste_overdue(db).all()}
        assert over in cockpit and edge not in cockpit   # 修前两条都在
        expected = db.query(MedicalWaste).filter(MedicalWaste.status != "handed_over",
                                                 MedicalWaste.collected_date < (today - timedelta(days=2)).isoformat()).count()
        assert medwaste_overdue_scan(db)[0] == expected
