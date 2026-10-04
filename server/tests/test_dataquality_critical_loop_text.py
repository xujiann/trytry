"""数据质控 QC007「危急值报告须闭环至处置反馈」的违规说明印中文状态文案，不印状态码（P2-1366，第四十批扫描 AD3-11）。

`dataquality._check_critical_closed_loop` 原先写「危急值闭环状态为 notified / acknowledged / 未回填，未达处置反馈（resolved）」：
状态码原样印给质控人员看，存量危急报告（迁移前）的空串写「未回填」，像是要补录数据。P2-1026 已把危急值页与指标导出的状态
文案统一取 `exams.CRITICAL_STATUS_NAMES`、存量空串按「已通知」写（M-1 整改：确认接收两态都收），这一处漏了。

修法同 P2-1026：取 `CRITICAL_STATUS_NAMES`，空串按「已通知」，resolved 写「已处置」。
"""
import inspect
import re

import pytest

from app.database import SessionLocal
from app.models import ExamReport, ExamRequest, User
from app.routers import dataquality
from app.routers.exams import CRITICAL_STATUS_NAMES


@pytest.fixture(scope="module")
def reports(client, admin):
    """已通知、已确认、已处置各一份危急值报告，再加一份存量（迁移前 critical_status=''）的。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21366 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21366 患者", "id_card": "330106197211133017", "gender": "女"})
    assert patient.status_code in (200, 201), patient.text
    patient_id = patient.json()["id"]
    ids = {}
    for status in ("notified", "acknowledged", "resolved"):
        req = client.post("/api/exams", headers=admin, json={
            "patient_id": patient_id, "from_org_id": org, "center_type": "lab", "item_code": f"P21366-{status}",
            "item_name": "血钾"}).json()
        assert client.post(f"/api/exams/{req['id']}/claim", headers=admin).status_code == 200
        rep = client.post(f"/api/exams/{req['id']}/report", headers=admin, json={
            "conclusion": "血钾 7.1 mmol/L", "critical": True})
        assert rep.status_code == 201, rep.text
        ids[status] = rep.json()["id"]
        if status != "notified":
            assert client.post(f"/api/exams/reports/{ids[status]}/acknowledge", headers=admin).status_code == 200
        if status == "resolved":
            assert client.post(f"/api/exams/reports/{ids[status]}/resolve", headers=admin,
                               json={"note": "已处置"}).status_code == 200
    with SessionLocal() as db:   # 存量危急报告：迁移前没有闭环状态
        creator = db.query(User.id).filter(User.username == "admin").scalar()
        req = ExamRequest(patient_id=patient_id, from_org_id=org, center_type="lab", item_code="P21366-legacy",
                          item_name="血钙", status="reported", created_by=creator)
        db.add(req)
        db.flush()
        legacy = ExamReport(request_id=req.id, conclusion="血钙 1.5 mmol/L", critical=True, critical_status="")
        db.add(legacy)
        db.commit()
        ids["legacy"] = legacy.id
    return ids


def test_违规说明印中文状态文案_存量空串按已通知(client, admin, reports):
    resp = client.get("/api/dataquality/run?rule_code=QC007", headers=admin)
    assert resp.status_code == 200, resp.text
    messages = {i["record_id"]: i["message"] for i in resp.json()["items"] if i["rule_code"] == "QC007"}
    assert messages == {   # 修前「危急值闭环状态为 notified / acknowledged / 未回填，未达处置反馈（resolved）」
        reports["notified"]: "危急值闭环状态为已通知，未达处置反馈（已处置）",
        reports["acknowledged"]: "危急值闭环状态为已确认，未达处置反馈（已处置）",
        reports["legacy"]: "危急值闭环状态为已通知，未达处置反馈（已处置）",
    }   # 已处置的那份不算违规
    for message in messages.values():
        assert not re.search(r"[A-Za-z]", message) and "未回填" not in message


def test_文案取危急值页同一张表():
    """措辞取 `exams.CRITICAL_STATUS_NAMES`（P2-1026 起危急值页、指标导出同一张），不在这里另写一套。"""
    source = inspect.getsource(dataquality._check_critical_closed_loop)
    assert "CRITICAL_STATUS_NAMES" in source
    assert CRITICAL_STATUS_NAMES["resolved"] == "已处置"
