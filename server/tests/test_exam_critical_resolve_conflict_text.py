"""已处置的危急值再点「处置反馈」，报「须先确认接收后方可处置反馈」（P2-1712，第五十批扫描 AN2-6）。

`resolve_critical` 的两处 409（先判状态、条件 UPDATE 落空）原先都是这句固定文案。处置反馈是框内提交（P2-607），报错就写在框里：
页面没刷新、别人已先处置，医生再点「处置反馈」，框里写着「须先确认接收」，把人引去找根本不存在的「确认接收」按钮。同文件
`acknowledge_critical` 早就按 `CRITICAL_STATUS_NAMES` 报当前状态（同一份报告再点确认接收是「当前状态 已处置 不可确认接收」）。
修前实测：`resolve again (stale page) 409 {'detail': '须先确认接收后方可处置反馈'}`。

修法：条件 UPDATE 落空时 rollback 后 refresh，两处都按库里此刻的状态说——还没确认接收的（含迁移前的存量空串）照旧
「须先确认接收后方可处置反馈」，别的报「当前状态 X 不可处置反馈」。
"""
import itertools

import pytest

from app.database import SessionLocal
from app.models import CriticalAction, ExamReport, ExamRequest, User


@pytest.fixture(scope="module")
def new_report(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21712 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21712 患者", "id_card": "330127197102021712"}).json()["id"]
    seq = itertools.count(1)

    def make(status):
        with SessionLocal() as db:
            creator = db.query(User).filter_by(username="admin").one().id
            request = ExamRequest(patient_id=patient, from_org_id=org, center_type="lab", item_code=f"P21712-{next(seq)}",
                                  item_name="血钾", status="reported", created_by=creator)
            db.add(request)
            db.flush()
            report = ExamReport(request_id=request.id, conclusion="血钾 2.6 mmol/L", critical=True, critical_status=status)
            db.add(report)
            db.commit()
            return report.id

    return make


def _actions(report_id):
    with SessionLocal() as db:
        return [a.action for a in db.query(CriticalAction).filter_by(report_id=report_id).order_by(CriticalAction.id)]


def _resolve(client, admin, report_id, note="已补钾并复查"):
    return client.post(f"/api/exams/reports/{report_id}/resolve", headers=admin, json={"note": note})


def test_已处置的再点处置反馈_报当前状态已处置(client, admin, new_report):
    report_id = new_report("acknowledged")
    assert _resolve(client, admin, report_id).status_code == 200
    again = _resolve(client, admin, report_id, "页面没刷新又写了一句")
    assert again.status_code == 409, again.text
    assert again.json()["detail"] == "当前状态 已处置 不可处置反馈"   # 修前「须先确认接收后方可处置反馈」
    assert _actions(report_id) == ["处置反馈：已补钾并复查"]   # 轨迹不多一条


@pytest.mark.parametrize("status", ["notified", ""], ids=["已通知", "存量空串"])
def test_还没确认接收的照旧说须先确认接收(client, admin, new_report, status):
    report_id = new_report(status)
    resp = _resolve(client, admin, report_id)
    assert resp.status_code == 409 and resp.json()["detail"] == "须先确认接收后方可处置反馈", resp.text


def test_读到已确认之后别人先处置了_条件更新落空也按库里此刻的状态说(client, admin, new_report, monkeypatch):
    """这一路读到「已确认」之后、写入之前，另一路先处置并提交（时序钉法同 test_exam_critical_transition_race）。"""
    from app.routers import exams

    report_id = new_report("acknowledged")
    real = exams._report_visible_or_404

    def racing(db, rid, user, resource):
        report = real(db, rid, user, resource)   # 这一路手上的是「已确认」
        with SessionLocal() as other:
            other.get(ExamReport, rid).critical_status = "resolved"
            other.add(CriticalAction(report_id=rid, action="处置反馈：另一位医生已处置", actor="另一位医生"))
            other.commit()
        return report

    monkeypatch.setattr(exams, "_report_visible_or_404", racing)
    got = _resolve(client, admin, report_id)
    monkeypatch.undo()
    assert got.status_code == 409, got.text
    assert got.json()["detail"] == "当前状态 已处置 不可处置反馈"   # 修前「须先确认接收后方可处置反馈」
    assert _actions(report_id) == ["处置反馈：另一位医生已处置"]
