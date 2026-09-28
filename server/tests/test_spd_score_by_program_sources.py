"""按病种跑考核分时，「风险评估」「监测」两个取数源也按病种取（P2-690，第十七批「比率分子分母」扫描 U3-8）。

`collect_metrics_batch` 的 `prog()` 给任务、转诊、上报、建档、目标池都加了病种条件，评估与监测却只按患者取：
同时管高血压与糖尿病的患者，8 月只做过高血压评估，按糖尿病跑「风险评估完成率」是 100%；糖尿病的「指标控制
达标率」拿的是血压读数。专家工作台的评估人次早就按病种筛（P2-552）。不带病种跑分的情形不变。
"""
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.spd.models import SpdAssessment, SpdEnrollment, SpdIndicator, SpdMeasurement, SpdScale
from app.spd.routers.assess import collect_metrics_batch

HTN, DM = "P2690H", "P2690D"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2690 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={"name": "P2690 两病共管", "id_card": "330106196909090771"})
    assert patient.status_code in (200, 201), patient.text
    pid = patient.json()["id"]
    at = datetime(2026, 8, 15, 9, 0)
    with SessionLocal() as db:
        scale_id = db.query(SpdScale.id).order_by(SpdScale.id).first()[0]
        for program in (HTN, DM):
            db.add(SpdEnrollment(patient_id=pid, program_code=program, org_id=org, status="active",
                                 created_at=datetime(2026, 7, 1, 9, 0)))
        db.add(SpdAssessment(patient_id=pid, scale_id=scale_id, program_code=HTN, created_at=at))   # 只做过高血压评估
        db.add_all([SpdMeasurement(patient_id=pid, program_code=HTN, metric="bp_sys", value=v, level="high",
                                   measured_at=at) for v in (170, 165)])                              # 两次血压，都偏高
        db.commit()
    return org


def _metrics(org, source, program):
    indicator = SpdIndicator(code=f"P2690_{source}", name=source, data_source=source, object_type="org")
    with SessionLocal() as db:
        return collect_metrics_batch(db, indicator, "org", [org], "2026-08", program)[org]


def test_按糖尿病跑分_高血压评估不算已评估(world):
    assert _metrics(world, "assessment", DM) == {"enrolled": 1.0, "assessed": 0.0}   # 修前 assessed 1：100%
    assert _metrics(world, "assessment", HTN) == {"enrolled": 1.0, "assessed": 1.0}


def test_按糖尿病跑分_血压读数不进达标率(world):
    assert _metrics(world, "measurement", DM) == {"total": 0.0, "normal": 0.0, "abnormal": 0.0}   # 修前 2 条异常
    assert _metrics(world, "measurement", HTN) == {"total": 2.0, "normal": 0.0, "abnormal": 2.0}


def test_不带病种跑分照旧(world):
    assert _metrics(world, "assessment", "") == {"enrolled": 1.0, "assessed": 1.0}
    assert _metrics(world, "measurement", "")["total"] == 2.0
