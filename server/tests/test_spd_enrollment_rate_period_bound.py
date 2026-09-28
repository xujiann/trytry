"""慢专病考核「纳管率」的分母（目标池）与分子同一个期末（P2-688，第十七批「比率分子分母」扫描 U3-1）。

分子只数期末之前建的档（`through_day(SpdEnrollment.created_at, end)`），分母取目标池里「目标 / 已纳管」的全部——
不设上界。补跑往期时，期末之后才入池的人全进了分母：8 月里入池 2 人、签了 2 人，9 月又入池 4 人（签了 1 个），
9 月里补跑 8 月是 2 / 6 = 33%，8 月底跑是 100%；越晚跑分越低，同一期永远复现不出来。机构绩效的存量分母早就写着
「必须设上界……否则历史分数会随新入组不断漂移」（`performance.py`）。
入池之后状态怎么变仍按现在的状态算——那是 P2-670 待裁定的另一半，本条不动。
"""
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.spd.models import SpdCandidate, SpdEnrollment, SpdIndicator
from app.spd.routers.assess import collect_metrics_batch

PROGRAM = "P2688X"
ID_CARDS = ["330106196707070019", "330106196707070027", "330106196707070035",
            "330106196707070043", "330106196707070051", "33010619670707006X"]


@pytest.fixture(scope="module")
def org(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2688 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patients = []
    for i, id_card in enumerate(ID_CARDS):
        resp = client.post("/api/patients", headers=admin, json={"name": f"P2688 居民{i}", "id_card": id_card})
        assert resp.status_code in (200, 201), resp.text
        patients.append(resp.json()["id"])
    pooled = [("enrolled", "2026-08-10"), ("enrolled", "2026-08-10"),               # 8 月入池、8 月签约
              ("target", "2026-09-05"), ("target", "2026-09-05"), ("target", "2026-09-05"),  # 9 月才入池
              ("enrolled", "2026-09-06")]                                           # 9 月入池并签约
    with SessionLocal() as db:
        for patient, (status, day) in zip(patients, pooled):
            at = datetime.fromisoformat(f"{day} 09:00:00")
            db.add(SpdCandidate(patient_id=patient, program_code=PROGRAM, status=status, org_id=org, created_at=at))
            if status == "enrolled":
                db.add(SpdEnrollment(patient_id=patient, program_code=PROGRAM, org_id=org, status="active",
                                     created_at=at))
        db.commit()
    return org


def _rate_inputs(org, period):
    indicator = SpdIndicator(code="P2688", name="纳管率", data_source="enrollment", object_type="org")
    with SessionLocal() as db:
        return collect_metrics_batch(db, indicator, "org", [org], period, PROGRAM)[org]


def test_补跑往期_期末之后才入池的人不进分母(org):
    assert _rate_inputs(org, "2026-08") == {"enrolled": 2.0, "target": 2.0, "high_risk": 0.0}   # 修前 target 6：33%


def test_当期照旧(org):
    assert _rate_inputs(org, "2026-09") == {"enrolled": 3.0, "target": 6.0, "high_risk": 0.0}
