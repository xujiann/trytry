"""公式变量「在管慢病人数」是期末存量，设期末上界：查上个月不数今天新建的档案（P2-766，第二十批「同一个数，多处口径」
扫描 M1-9）。

同一期的绩效考核早就设了上界（`performance.py`：「必须设上界，否则历史分数会随新入组不断漂移」）；公式变量
`chronic_patients`（期末综合绩效报告、自定义绩效公式取它）原先不设：两份慢病档案今天建，上个月的绩效考核分母 0，
期末综合绩效报告里「在管慢病人数」却是 2。运行效率的床位数同样没有上界，但床位没有开放历史（`created_at` 是录入时刻，
存量导入的往期会被截成 0 床），那一处不在此列。
"""
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.models import ChronicPatient
from app.routers.analytics import build_variable_index

BEFORE, AFTER = datetime(2026, 8, 15, 9), datetime(2026, 10, 10, 9)   # 查 2026-09：一个在期末之前、一个在期末之后


@pytest.fixture(scope="module")
def org(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2766 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patients = [client.post("/api/patients", headers=admin, json={
        "name": f"P2766 患者{i}", "id_card": f"33010619600505{2766 + i:04d}"}).json()["id"] for i in range(2)]
    with SessionLocal() as db:
        for patient, at in zip(patients, (BEFORE, AFTER)):
            db.add(ChronicPatient(patient_id=patient, disease="hypertension", managed_by_org_id=org, created_at=at))
        db.commit()
    return org


def test_往期只数期末之前建档的_之后的期照数(org):
    with SessionLocal() as db:
        assert build_variable_index(db, "2026-09")[org]["chronic_patients"] == 1.0   # 修前 2：数进了 10 月才建的档
        assert build_variable_index(db, "2026-10")[org]["chronic_patients"] == 2.0
        assert build_variable_index(db, "2026-07")[org]["chronic_patients"] == 0.0   # 修前 2
