"""出院小结的住院天数与居民端、成本核算、DRG 同一口径：出入院日期差，当日入当日出计 1 天（P2-204）。

打印版原先是时刻差的整天数 + 1：9/1 10:00 入、9/5 11:00 出印「5 天」，居民端「我的住院」、DRG 都是 4 天；
9/1 08:00 入、9/2 09:00 出印 2 天、别处 1 天。`portal.py` 自己写着「免得同一次住院在三个地方显示三个天数」，
而出院小结是医疗文书。
"""
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.models import Admission, Bed, Organization, Patient, User, Ward


@pytest.fixture(scope="module")
def admissions(client):
    db = SessionLocal()
    try:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        org = Organization(name="P2204 县医院", org_type="lead_hospital", level="county")
        db.add(org)
        db.flush()
        ward = Ward(org_id=org.id, name="P2204 内科")
        db.add(ward)
        db.flush()
        ids = []
        for n, (admitted, discharged) in enumerate(((datetime(2026, 9, 1, 10, 0), datetime(2026, 9, 5, 11, 0)),
                                                    (datetime(2026, 9, 1, 8, 0), datetime(2026, 9, 2, 9, 0)),
                                                    (datetime(2026, 9, 3, 8, 0), datetime(2026, 9, 3, 17, 0)))):
            patient = Patient(ehc_no=f"EHC-P2204-{n}", name=f"P2204 患者{n}", id_card=f"33010619880808{1600 + n}")
            bed = Bed(ward_id=ward.id, bed_no=str(n))
            db.add_all([patient, bed])
            db.flush()
            adm = Admission(patient_id=patient.id, org_id=org.id, ward_id=ward.id, bed_id=bed.id, status="discharged",
                            admitted_at=admitted, discharged_at=discharged, created_by=admin_id)
            db.add(adm)
            db.flush()
            ids.append(adm.id)
        db.commit()
        return ids
    finally:
        db.close()


@pytest.mark.parametrize("index, days", [(0, 4), (1, 1), (2, 1)])   # 修前 5 / 2 / 1
def test_住院天数按出入院日期差_当日入出计1天(client, admin, admissions, index, days):
    html = client.get(f"/api/print/discharge-summaries/{admissions[index]}", headers=admin).text
    assert f'<td class="k">住院天数</td><td>{days} 天</td>' in html
