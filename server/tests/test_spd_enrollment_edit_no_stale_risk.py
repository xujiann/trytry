"""慢专病「调整纳管档案」每次都把页面载入时的风险分层和阶段送回，其间评估判出的极高危被改回低危、路径推进的阶段被改回
（P2-960，第二十七批「丢失更新：编辑时把页面载入的整行值写回」扫描 G1-1）。

弹窗标题写着「留空的项不改」，可风险分层下拉按列表载入时的值预填、没有「不改」，提交恒送 `risk_level`；阶段也按载入值预填、
非空就送。页面开着的时候另一位医护做了评估（极高危，回写档案并派高危复诊），或办结任务把路径推进了阶段，原页面点「调整」
只填个下次随访日，档案就被改回载入时的低危和旧阶段——已派出的高危复诊还挂着，按极高危筛在管档案查不到这个人。

修法：风险分层缺省「（不改）」、标签写明载入时的值；风险与阶段都只在和载入值不同时才送（与 P2-920「只送改过的字段」同一口径）。
"""
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")
B = "/api/spd"


def _dialog():
    start = PAGE.index('spdModal("调整纳管档案（留空的项不改）"')
    return PAGE[PAGE.rindex("if (enrEdit) {", 0, start):PAGE.index("/api/spd/enrollments/${enrEdit.dataset.enrEdit}", start)]


def test_风险分层缺省不改_风险与阶段只在改了才送():
    dialog = _dialog()
    risk = dialog[dialog.index('name: "risk_level"'):]
    risk = risk[:risk.index("] },")]
    assert 'value: ""' in risk and "（不改）" in risk, risk   # 修前按载入值预填、没有「不改」
    assert "const body = {};" in dialog   # 修前 { risk_level: form.risk_level } 恒送
    assert "form.risk_level !== loadedRisk" in dialog
    assert "form.stage !== (enrEdit.dataset.stage" in dialog


@pytest.fixture(scope="module")
def enrollment(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2960 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2960 患者", "id_card": "33010619660505005X", "birth_date": "1966-05-05"}).json()["id"]
    made = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org, "risk_level": "low"})
    assert made.status_code == 201, made.text
    return made.json()["id"]


def test_只送下次随访日_评估回写的风险分层不被改回(client, admin, enrollment):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment

    with SessionLocal() as db:   # 页面开着的时候，别人评估出了极高危（评估回写档案的效果）
        db.get(SpdEnrollment, enrollment).risk_level = "very_high"
        db.commit()
    resp = client.patch(f"{B}/enrollments/{enrollment}", headers=admin, json={"next_followup_at": "2026-10-20"})
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        row = db.get(SpdEnrollment, enrollment)
        assert (row.risk_level, row.next_followup_at) == ("very_high", "2026-10-20")
