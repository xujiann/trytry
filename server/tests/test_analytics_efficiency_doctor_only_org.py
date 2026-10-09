"""有医师、本期没有业务量的机构，运行效率照出一行，公式变量「在岗医师数」照取（P2-1703，第五十批扫描 AN3-1）。

运行效率的候选机构原先是 `床位 | 诊疗人次 | 占用床日 | 出院人次` 四个字典的键取并集，唯独漏了医师数——没床位、本期又没有
门诊和住院的机构（典型是只做随访的村卫生室），医师数照算出来了却不进结果行：运行效率页上整行消失，期末综合绩效报告的
公式变量 `doctors` 取 0（`build_variable_index` 从这里取），`chronic_patients / doctors` 这类人均公式除零按 0 计。
修前实测：西村卫生室 2 名在岗乡村医生、30 位在管慢病、本月无就诊，运行效率本月 `[]`，报告里两条公式都是 0.0。
"""
from datetime import datetime

import pytest

from app import clock
from app.database import SessionLocal
from app.models import ChronicPatient, Employee, Organization, Patient

LONG_AGO = datetime(2020, 1, 1)   # 建档、入职都在很久以前：本条不涉及按期末取（那是 P2-1705）


@pytest.fixture(scope="module")
def village(client, admin):
    with SessionLocal() as db:
        org = Organization(name="P21703 西村卫生室", org_type="village", level="village")
        db.add(org)
        db.flush()
        db.add_all([Employee(org_id=org.id, name=f"P21703 村医{i}", position="乡村医生", created_at=LONG_AGO)
                    for i in range(2)])
        for i in range(30):
            patient = Patient(ehc_no=f"EHC-P21703-{i}", name=f"P21703 慢病{i}", id_card=f"33010619600505{i:04d}")
            db.add(patient)
            db.flush()
            db.add(ChronicPatient(patient_id=patient.id, disease="hypertension", managed_by_org_id=org.id,
                                  created_at=LONG_AGO))
        db.commit()
        org_id = org.id
    for key, expression in (("p21703_chr_per_doc", "chronic_patients / doctors"), ("p21703_docs", "doctors")):
        resp = client.post("/api/analytics/formulas", headers=admin,
                           json={"key": key, "name": key, "expression": expression, "weight": 0})
        assert resp.status_code == 201, resp.text
    return org_id


def test_只有医师没有业务的机构照出一行(client, admin, village):
    period = clock.today().isoformat()[:7]
    rows = client.get(f"/api/analytics/efficiency?period={period}", headers=admin)
    assert rows.status_code == 200, rows.text
    row = next((r for r in rows.json() if r["org_id"] == village), None)
    assert row is not None, rows.json()   # 修前整行消失
    assert row["doctors"] == 2
    # 其余各数照现有口径为 0
    assert (row["beds"], row["visits"], row["discharges"], row["visits_per_doctor_per_day"]) == (0, 0, 0, 0.0)


def test_报告里在岗医师数与人均公式照取(client, admin, village):
    period = clock.today().isoformat()[:7]
    report = client.get(f"/api/analytics/performance-report?period={period}", headers=admin)
    assert report.status_code == 200, report.text
    org = next(o for o in report.json()["orgs"] if o["org_id"] == village)
    values = {item["key"]: item["value"] for item in org["items"]}
    assert values == {"p21703_chr_per_doc": 15.0, "p21703_docs": 2.0}   # 修前 0.0 / 0.0
