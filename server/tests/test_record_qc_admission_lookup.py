"""病历质控抽检的「对象ID」按另一套编号查：病案首页一支按病案首页自己的编号（case_summaries.id）取，页面上各处给人看的却是
住院号（P2-998，第二十八批「编号、单号与流水号」扫描 F3-4）。

`create_record_qc` 按 `db.get(CaseSummary, target_id)` 取；病案首页在其他所有入口都按住院号（出院、打印件单据号 BA{住院号}、
「病案首页（住院 N）」弹窗），case_summaries.id 在任何页面上都不出现。首页按出院先后建，编号次序与住院号不同：给住院号 1 的
首页打丙级，结果挂到住院号 2 那位患者的首页上，201、没有提示。门急诊一支按就诊号，同页「最近病历」表第一列是病历号、第二列
才是就诊号，表头只写「ID / 就诊」。

修法：病案首页可按住院号（`admission_id`）抽检，后端换成病案首页编号落库（存量的对象号含义不变）；两者都给的要对得上；
页面病案首页一支送住院号，「最近病历」表头写明病历ID / 就诊ID。回执形状不变。
"""
from datetime import datetime
from pathlib import Path

import pytest

from app.database import SessionLocal

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def world(client, admin):
    from app.models import Admission, Bed, CaseSummary, User, Ward

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2998 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patients = [client.post("/api/patients", headers=admin, json={
        "name": f"P2998 住院{i}", "id_card": f"33010619740404{2998 + i:04d}"}).json()["id"] for i in range(2)]
    with SessionLocal() as db:
        creator = db.query(User.id).filter(User.username == "admin").scalar()
        ward = Ward(org_id=org, name="P2998 病区")
        db.add(ward)
        db.flush()
        beds = [Bed(ward_id=ward.id, bed_no=f"P2998-{i}") for i in range(2)]
        db.add_all(beds)
        db.flush()
        admissions = []
        for i in range(2):
            adm = Admission(patient_id=patients[i], org_id=org, ward_id=ward.id, bed_id=beds[i].id,
                            status="discharged", admitted_at=datetime(2026, 9, 1 + i, 4), created_by=creator,
                            discharged_at=datetime(2026, 9, 10, 4))
            db.add(adm)
            db.flush()
            admissions.append(adm.id)
        summaries = {}
        for adm_id in reversed(admissions):   # 后入院的先出院、先建首页：编号次序与住院号相反
            summary = CaseSummary(admission_id=adm_id, discharge_diagnosis="P2998 诊断")
            db.add(summary)
            db.flush()
            summaries[adm_id] = summary.id
        db.commit()
    return {"admissions": admissions, "summaries": summaries}


def test_病案首页按住院号抽检_挂到这次住院的首页(client, admin, world):
    first = world["admissions"][0]
    resp = client.post("/api/quality/record-qc", headers=admin, json={
        "target_type": "case_summary", "admission_id": first, "score": 60, "defects": "出院诊断不全"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["target_id"] == world["summaries"][first]   # 修前：照住院号当首页编号，挂到另一位患者的首页上
    assert list(resp.json()) == ["id", "target_type", "target_id", "score", "grade", "defects", "qc_by"]


def test_首页编号与住院号对不上_422_门急诊不收住院号(client, admin, world):
    first, second = world["admissions"]
    clash = client.post("/api/quality/record-qc", headers=admin, json={
        "target_type": "case_summary", "target_id": world["summaries"][second], "admission_id": first, "score": 90})
    assert clash.status_code == 422, clash.text
    wrong_kind = client.post("/api/quality/record-qc", headers=admin, json={
        "target_type": "encounter", "admission_id": first, "score": 90})
    assert wrong_kind.status_code == 422, wrong_kind.text
    nothing = client.post("/api/quality/record-qc", headers=admin, json={"target_type": "encounter", "score": 90})
    assert nothing.status_code == 422, nothing.text


def test_按首页编号的老调用照旧(client, admin, world):
    second = world["admissions"][1]
    resp = client.post("/api/quality/record-qc", headers=admin, json={
        "target_type": "case_summary", "target_id": world["summaries"][second], "score": 92})
    assert resp.status_code == 201 and resp.json()["target_id"] == world["summaries"][second], resp.text


def test_页面病案首页一支送住院号():
    handler = PAGE[PAGE.index('$("#qc-rec-form").onsubmit'):]
    handler = handler[:handler.index("};")]
    assert 'body.target_type === "case_summary"' in handler and "body.admission_id = body.target_id" in handler
    assert '["病历ID", "就诊ID", "医师"' in PAGE
