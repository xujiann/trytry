"""危急值闭环的两步（确认接收、处置反馈）是锁外读改写：并发时两路都 200，处置轨迹记两条（P2-285）。

`acknowledge_critical` / `resolve_critical` 原先「读 critical_status → 判 → 赋值 → 记一条处置轨迹 → commit」。两位
医生同时点「确认接收」（或双击），两路都读到「已通知」、都 200，轨迹里两条「确认接收」；处置反馈同理——两句反馈都记下，
闭环记成两次。平台其余状态迁移早已改成带状态条件的 UPDATE（P2-109 起），这两步的列叫 `critical_status` 而不是 `status`，
按 `.status` 认形状的扫描一直没看见它们。

修法：`_move_critical`——判定与写入压进同一条 `WHERE critical_status IN (前态)` 的 UPDATE，后到的一路 409、与顺序请求
同一句，轨迹恰好一条。这里把「一路读到前态之后、写入之前，另一路先提交了」钉成确定的时序：在两处都先调、恰在读取之后的
归属判定里插进另一路的提交。
"""
import itertools

import pytest

from app.database import SessionLocal
from app.models import CriticalAction, ExamReport, ExamRequest


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2285 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2285 患者", "id_card": "330127197309092285"}).json()["id"]
    seq = itertools.count(1)

    def new_report(status):
        with SessionLocal() as db:
            request = ExamRequest(patient_id=patient, from_org_id=org, center_type="lab", item_code=f"P2285-{next(seq)}",
                                  item_name="血钾", status="reported", created_by=1)
            db.add(request)
            db.flush()
            report = ExamReport(request_id=request.id, conclusion="血钾 7.1 mmol/L", critical=True,
                                critical_status=status)
            db.add(report)
            db.commit()
            return report.id

    return {"new_report": new_report}


def _state(report_id):
    with SessionLocal() as db:
        return (db.get(ExamReport, report_id).critical_status,
                [a.action for a in db.query(CriticalAction).filter_by(report_id=report_id).order_by(CriticalAction.id)])


def _other_route_first(monkeypatch, status, action):
    """这一路读到报告（前态）之后，另一路先把同一步办完并提交。"""
    from app.routers import exams

    real = exams._report_visible_or_404

    def racing(db, report_id, user, resource):
        report = real(db, report_id, user, resource)   # 这一路手上的是前态
        with SessionLocal() as other:
            other.get(ExamReport, report_id).critical_status = status
            other.add(CriticalAction(report_id=report_id, action=action, actor="另一位医生"))
            other.commit()
        return report

    monkeypatch.setattr(exams, "_report_visible_or_404", racing)


def test_两人同时确认接收_只成一路_轨迹一条(client, admin, world, monkeypatch):
    report_id = world["new_report"]("notified")
    _other_route_first(monkeypatch, "acknowledged", "医师确认接收危急值通知")
    got = client.post(f"/api/exams/reports/{report_id}/acknowledge", headers=admin)
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200
    assert got.json()["detail"] == "当前状态 已确认 不可确认接收"   # 与顺序重复确认同一句，按库里此刻的状态说
    assert _state(report_id) == ("acknowledged", ["医师确认接收危急值通知"])   # 修前两条


def test_两人同时处置反馈_只成一路_反馈一条(client, admin, world, monkeypatch):
    report_id = world["new_report"]("acknowledged")
    _other_route_first(monkeypatch, "resolved", "处置反馈：已电话通知并复查")
    got = client.post(f"/api/exams/reports/{report_id}/resolve", headers=admin, json={"note": "已补钾"})
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200：两句反馈都记下
    assert _state(report_id) == ("resolved", ["处置反馈：已电话通知并复查"])


def test_不并发时闭环照常(client, admin, world):
    report_id = world["new_report"]("notified")
    assert client.post(f"/api/exams/reports/{report_id}/acknowledge", headers=admin).json()["critical_status"] == "acknowledged"
    resolved = client.post(f"/api/exams/reports/{report_id}/resolve", headers=admin, json={"note": "已补钾"})
    assert resolved.status_code == 200 and resolved.json()["critical_status"] == "resolved", resolved.text
    again = client.post(f"/api/exams/reports/{report_id}/acknowledge", headers=admin)
    assert again.status_code == 409 and again.json()["detail"] == "当前状态 已处置 不可确认接收", again.text
