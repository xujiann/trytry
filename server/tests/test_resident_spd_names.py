"""居民端慢专病页把编码原样显示给居民（P2-372）。

- 档案时间轴的随访标题拼的是场景码：「inpatient随访」（`FOLLOWUP_SCENE_NAMES` 早就有）；
- 居家监测的来源只认 device，公卫随访同步、院内系统、POCT 的都显示成「手工记录」（`MEASUREMENT_SOURCE_NAMES` 早就有）；
- 咨询「病种」、服务申请「申请病种」显示病种编码，评估「量表」显示量表编码，历程里的路径显示模板编码与节点 key；
- 居民自查的结论只翻译了低 / 中 / 高危，量表给「极高危」的显示成「风险等级：very_high」。

修法：出参带上中文名（`program_name` / `scale_name` / `template_name` / `current_node_name` / `source_name`），页面显示名称；
自查结论用页面现成的风险表（含极高危）。
"""
from datetime import datetime
from pathlib import Path

import pytest

from app.database import SessionLocal
from conftest import login

B = "/api/portal/spd"
SRC = (Path(__file__).resolve().parents[1] / "app" / "static" / "m" / "m.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def world(client):
    from app.spd.models import (SpdAssessment, SpdConsult, SpdEnrollment, SpdFollowupRecord, SpdMeasurement,
                                SpdPathInstance, SpdPathNode, SpdPathTemplate, SpdProgram, SpdScale, SpdServiceApply)

    h = login(client, "admin", "admin123")
    org = client.post("/api/organizations", headers=h, json={
        "name": "P2372 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    me = client.post("/api/patients", headers=h, json={
        "name": "P2372 居民", "id_card": "330199197202022372", "gender": "男", "birth_date": "1972-02-02",
        "phone": "13800002372"}).json()
    enrolled = client.post("/api/spd/enrollments", headers=h, json={
        "patient_id": me["id"], "program_code": "hypertension", "org_id": org})
    assert enrolled.status_code == 201, enrolled.text
    with SessionLocal() as db:
        program = db.query(SpdProgram).filter(SpdProgram.code == "hypertension").one()
        template = SpdPathTemplate(program_id=program.id, code="p2372_path", name="P2372 高血压管理路径",
                                   status="published")
        db.add(template)
        db.flush()
        db.add(SpdPathNode(template_id=template.id, key="assess", name="首诊评估"))
        db.add(SpdPathInstance(enrollment_id=enrolled.json()["id"], template_id=template.id,
                               template_code="p2372_path", current_node_key="assess", status="running"))
        db.add(SpdFollowupRecord(patient_id=me["id"], program_code="hypertension", scene="inpatient",
                                 org_id=org, planned_at="2026-09-01", executed_at="2026-09-02", status="done",
                                 channel="phone", result="P2372 出院随访"))
        db.add(SpdMeasurement(patient_id=me["id"], metric="bp_sys", value=150, unit="mmHg", level="high",
                              source="publichealth", measured_at=datetime(2026, 9, 20, 8, 0)))
        db.add(SpdServiceApply(patient_id=me["id"], program_code="diabetes", status="pending"))
        db.add(SpdConsult(patient_id=me["id"], program_code="hypertension", status="open"))
        scale = db.query(SpdScale).filter(SpdScale.code == "assess_risk_common").order_by(SpdScale.id.desc()).first()
        db.add(SpdAssessment(patient_id=me["id"], scale_id=scale.id, scale_code="assess_risk_common", score=3,
                             risk_level="mid"))
        db.commit()
        assert db.query(SpdEnrollment).filter(SpdEnrollment.patient_id == me["id"]).count() == 1
    code = client.post("/api/portal/auth/sms/code", json={"phone": me["phone"]}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": me["phone"], "code": code})
    assert token.status_code == 200, token.text
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
    bound = client.post("/api/portal/auth/realname", headers=headers,
                        json={"name": me["name"], "id_card": me["id_card"]})
    assert bound.status_code in (200, 201, 409), bound.text   # 409：登录时按手机号已自动绑上
    return headers


def test_档案时间轴的随访标题是场景名(client, world):
    got = client.get(f"{B}/archive", headers=world)
    assert got.status_code == 200, got.text
    titles = [t["title"] for t in got.json()["timeline"] if t["kind"] == "followup"]
    assert "出院随访" in titles and "inpatient随访" not in titles, titles


def test_出参带中文名(client, world):
    rows = client.get(f"{B}/measurements", headers=world).json()
    assert [r["source_name"] for r in rows if r["source"] == "publichealth"] == ["公卫随访同步"]
    applies = client.get(f"{B}/service-applies", headers=world).json()
    assert [a["program_name"] for a in applies] == ["2型糖尿病"]
    consults = client.get(f"{B}/consults", headers=world).json()
    assert [c["program_name"] for c in consults] == ["高血压"]
    assessments = client.get(f"{B}/assessments", headers=world).json()
    assert [a["scale_name"] for a in assessments] == ["慢专病综合风险评估量表"]
    paths = client.get(f"{B}/journey", headers=world).json()["programs"][0]["paths"]
    assert [(p["template_name"], p["current_node_name"]) for p in paths] == [("P2372 高血压管理路径", "首诊评估")]


def test_页面显示名称而不是编码():
    for shown in ("r.source_name", "a.scale_name", "i.template_name", "i.current_node_name",
                  "c.program_name", "a.program_name"):
        assert shown in SRC, shown
    assert '"设备采集" : "手工记录"' not in SRC   # 修前只认 device
    assert "{ low: \"低危\", mid: \"中危\", high: \"高危\" }[r.risk_level]" not in SRC   # 修前自查结论缺极高危
