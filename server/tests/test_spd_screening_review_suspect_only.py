"""筛查复核只收「疑似」的（P2-591，第十二批「计数 vs 清单」扫描 Z1-1）。

工作台的「待复核」只数疑似（`result == "suspect"` 且未复核），筛查清单原先却给每条未复核的筛查都画了确认 / 排除，
复核接口也照单全收：对命中排除规则的一条点「确认」，候选从「排除」改回「目标」——排除规则挡在门外的人（比如未成年）
进了目标人群；未见异常的没有候选，确认了只是记一笔「已复核」。
"""
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.spd.models import SpdCandidate, SpdScreening

B = "/api/spd"
PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2591 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    cases = (("suspect", "suspect"), ("excluded", "excluded"), ("normal", None))
    patients = {result: client.post("/api/patients", headers=admin, json={
        "name": f"P2591 {result}", "id_card": f"33012720100101259{n}"}).json()["id"]
        for n, (result, _) in enumerate(cases)}
    ids = {}
    with SessionLocal() as db:
        for result, candidate_status in cases:
            patient = patients[result]
            screening = SpdScreening(patient_id=patient, program_code="hypertension", source="active",
                                     org_id=org, result=result)
            db.add(screening)
            if candidate_status:
                db.add(SpdCandidate(patient_id=patient, program_code="hypertension", status=candidate_status,
                                    org_id=org))
            db.flush()
            ids[result] = (screening.id, patient)
        db.commit()
    return ids


def _candidate_status(patient_id: int) -> str | None:
    with SessionLocal() as db:
        row = db.query(SpdCandidate).filter(SpdCandidate.patient_id == patient_id).one_or_none()
        return row.status if row else None


@pytest.mark.parametrize("result", ["excluded", "normal"])
def test_不是疑似的筛查不收复核(client, admin, world, result):
    screening_id, patient = world[result]
    resp = client.post(f"{B}/screenings/{screening_id}/review", headers=admin, json={"review_result": "confirmed"})
    assert resp.status_code == 409, resp.text   # 修前 200
    with SessionLocal() as db:
        assert db.get(SpdScreening, screening_id).reviewed is False
    assert _candidate_status(patient) == ("excluded" if result == "excluded" else None)   # 修前排除的被改回 target


def test_疑似的照常复核进目标人群(client, admin, world):
    screening_id, patient = world["suspect"]
    resp = client.post(f"{B}/screenings/{screening_id}/review", headers=admin, json={"review_result": "confirmed"})
    assert resp.status_code == 200 and resp.json()["reviewed"] is True, resp.text
    assert _candidate_status(patient) == "target"


def test_清单只在疑似的未复核行上给复核按钮():
    start = PAGE.index("const drawScreenings = async () => {")
    body = PAGE[start:PAGE.index("const drawEnrollments", start)]
    assert '${s.reviewed || s.result !== "suspect" ? "—" :' in body   # 修前 `${s.reviewed ? "—" :`
    assert 's.result === "suspect" ? \'<span class="tag orange">待复核</span>\' : "—"' in body
