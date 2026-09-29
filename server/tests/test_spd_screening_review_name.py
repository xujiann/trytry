"""筛查清单的复核列显示后端给的中文，不再原样印 confirmed / excluded / pending（P2-797，第二十一批「页面给出的动作 vs 后端
允许的角色与状态」扫描 N4-7 的文案部分；CLAUDE.md §13「状态文案取自后端」）。

「待定」要不要算待复核、页面给不给「待定」入口是 P2-818（待裁定），不在本条。
"""
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.spd.models import SpdScreening

B = "/api/spd"
PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def screenings(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2797 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2797 患者", "id_card": "330102196001012797"}).json()["id"]
    with SessionLocal() as db:
        rows = [SpdScreening(patient_id=patient, program_code="hypertension", org_id=org, result="suspect",
                             risk_level="high", reviewed=False) for _ in range(4)]
        db.add_all(rows)
        db.commit()
        return patient, [r.id for r in rows]


def test_复核结论带中文名(client, admin, screenings):
    patient, ids = screenings
    for sid, result in zip(ids, ("confirmed", "excluded", "pending")):
        resp = client.post(f"{B}/screenings/{sid}/review", headers=admin, json={"review_result": result})
        assert resp.status_code == 200, resp.text
    rows = {r["id"]: r for r in client.get(f"{B}/screenings", headers=admin, params={"patient_id": patient}).json()}
    assert [rows[sid]["review_result_name"] for sid in ids] == ["确认", "排除", "待定", ""]   # 修前没有这个键


def test_复核列显示中文名():
    assert "esc(s.review_result_name || s.review_result)" in PAGE   # 修前 esc(s.review_result)
