"""危急值「修订」弹窗的结论必填、且预填页面载入时的结论：另一位医师刚修订过的结论被改回（P2-962，第二十七批「丢失更新」
扫描 G1-5）。

同一个修订框里「所见」写的是「留空不改」、危急标记缺省「不改」，唯独结论必填（`ReportAmend.conclusion`）、按列表载入时的值预填、
恒送。甲修订了结论（复测 7.2、已排除溶血、建议立即处置），乙在旧页面上只想补一段所见，一提交结论就回到甲修订之前的文本——
仍是危急值的，闭环状态随之复位为「已通知」、按旧结论重发通知；修订史里查得到，报告正文却已回退。

修法：结论不送就不改（至少要改结论、所见、危急标记中的一项，否则 422）；页面结论照旧预填，只在改了才送。
"""
from pathlib import Path

import pytest

CORE = (Path(__file__).resolve().parents[1] / "app" / "static" / "core.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def report_id(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2962 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2962 患者", "id_card": "330106197107070079", "gender": "男"}).json()["id"]
    req = client.post("/api/exams", headers=admin, json={
        "patient_id": patient, "from_org_id": org, "center_type": "lab", "item_code": "K-P2962",
        "item_name": "血钾(P2962)"}).json()
    client.post(f"/api/exams/{req['id']}/claim", headers=admin)
    made = client.post(f"/api/exams/{req['id']}/report", headers=admin, json={
        "finding": "血钾 6.9mmol/L", "conclusion": "血钾 6.9 mmol/L", "critical": True, "reported_by": "检验科"})
    assert made.status_code in (200, 201), made.text
    return made.json()["id"]


def _conclusion(client, admin, rid):
    from app.database import SessionLocal
    from app.models import ExamReport

    with SessionLocal() as db:
        return db.get(ExamReport, rid).conclusion


def test_只补所见_不送结论_别人刚修订的结论不回退(client, admin, report_id):
    amended = client.patch(f"/api/exams/reports/{report_id}", headers=admin, json={
        "conclusion": "复测 7.2，已排除溶血，建议立即处置", "reason": "甲复核"})
    assert amended.status_code == 200, amended.text
    finding_only = client.patch(f"/api/exams/reports/{report_id}", headers=admin, json={
        "finding": "血钾 6.9mmol/L；复测 7.2mmol/L", "reason": "乙补所见"})
    assert finding_only.status_code == 200, finding_only.text   # 修前 422：结论必填，页面只好把载入时的旧结论送回
    assert _conclusion(client, admin, report_id) == "复测 7.2，已排除溶血，建议立即处置"


def test_一项都不改_422(client, admin, report_id):
    resp = client.patch(f"/api/exams/reports/{report_id}", headers=admin, json={"reason": "空修订"})
    assert resp.status_code == 422, resp.text


def test_页面结论只在改了才送():
    at = CORE.index('spdModal("修订报告（改前值会连同理由留痕）"')
    block = CORE[at:CORE.index("/api/exams/reports/${amend}", at)]
    assert "required: true" not in block[:block.index('name: "finding"')]   # 修前结论必填
    assert "form.conclusion !== e.target.dataset.conclusion" in block
