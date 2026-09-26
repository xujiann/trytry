"""年度复评提醒：没填评估日期的按录入那天算，与「最近一次评估」同一个口径（P2-212）。

评估日期那一格是选填的，「最近一次评估」早就写明「没填日期的按录入那天」（P2-125），复评提醒却只看 `assessed_date`：
2024-06-01 录入、没填日期的评估，到 2026-09-26 一直不出「已超一年，应安排复评」；同一条填了日期就出。
"""
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.models import ElderlyAssessment, Patient


@pytest.fixture(scope="module")
def patients(client):
    with SessionLocal() as db:
        rows = {}
        for key, assessed, created in (("no_date", "", datetime(2024, 6, 1, 9, 0)),
                                       ("dated", "2024-06-01", datetime(2024, 6, 1, 9, 0)),
                                       ("recent", "", datetime(2026, 8, 1, 9, 0))):
            patient = Patient(ehc_no=f"EHC-P2212-{key}", name=f"P2212 {key}", id_card=f"33010619500101{len(rows) + 1640}")
            db.add(patient)
            db.flush()
            db.add(ElderlyAssessment(patient_id=patient.id, adl_score=95, assessed_date=assessed, created_at=created))
            rows[key] = patient.id
        db.commit()
        return rows


def test_没填日期的按录入那天出复评提醒(client, admin, patients):
    body = client.get("/api/eldercare/alerts?today=2026-09-26", headers=admin).json()
    due = {a["patient_id"] for a in body["alerts"] if a["alert_type"] == "reassess_due"}
    assert patients["no_date"] in due                          # 修前不在
    assert patients["dated"] in due and patients["recent"] not in due
