"""危急值闭环两步写接口的归属校验与留痕（P0-27）。

危急值闭环是「通知 → 确认接收 → 处置反馈」：报告一出，站内消息发给**申请机构**的医师，
由他们确认接收、做完处置再反馈。P0-21 已把处置轨迹的读接口收口（隔一跳按申请单的患者判
可见性并留痕，`_report_visible_or_404`），写侧两步没跟——2026-09-24 实测（排查 P1-71 名单时
发现，`exam_reports` 离患者隔一跳，名单的分母看不到它）：

- `POST /api/exams/reports/{id}/acknowledge`：乙院医生按报告号就把甲院的危急值「确认接收」了（200）；
- `POST /api/exams/reports/{id}/resolve`：接着「处置反馈」一落库，危急值就算闭环（200）——
  而乙院连这条危急值的处置轨迹都读不到（403）。

照读侧同一个判定补上：先判归属再判状态（无关机构对非危急报告拿到的是 403，不是 422）。
"""
import itertools

import pytest

from app.database import SessionLocal
from app.models import AccessLog, CriticalAction, ExamReport, ExamRequest


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def critical_world(client):
    """甲卫生院开的检验单出了危急值；乙院一名医师与该患者毫无关系。"""
    admin = _login(client, "admin", "admin123")
    a = client.post("/api/organizations",
                    json={"name": "危急值归属甲院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "危急值归属乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    for uname, org in (("crit_doc_a", a), ("crit_doc_b", b)):
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": "doctor", "org_id": org["id"]},
                    headers=admin)
    patient = client.post("/api/patients",
                          json={"name": "危急值归属患者", "id_card": "320000199202026674"},
                          headers=admin).json()
    seq = itertools.count(1)

    def new_report(critical=True, status="notified") -> int:
        db = SessionLocal()
        try:
            req = ExamRequest(patient_id=patient["id"], from_org_id=a["id"], center_type="lab",
                              item_code=f"K{next(seq)}", item_name="血钾", status="reported", created_by=1)
            db.add(req)
            db.flush()
            report = ExamReport(request_id=req.id, conclusion="血钾 7.1 mmol/L", critical=critical,
                                critical_status=status if critical else "")
            db.add(report)
            db.commit()
            return report.id
        finally:
            db.close()

    return {"doc_a": _login(client, "crit_doc_a"), "doc_b": _login(client, "crit_doc_b"),
            "patient_id": patient["id"], "new_report": new_report}


def _state(report_id: int) -> tuple[str, int]:
    db = SessionLocal()
    try:
        status = db.get(ExamReport, report_id).critical_status
        actions = db.query(CriticalAction).filter(CriticalAction.report_id == report_id).count()
        return status, actions
    finally:
        db.close()


def _logs(patient_id: int) -> int:
    db = SessionLocal()
    try:
        return db.query(AccessLog).filter(
            AccessLog.patient_id == patient_id, AccessLog.resource == "exam_critical_action"
        ).count()
    finally:
        db.close()


def test_无关机构不能替别家确认接收危急值(client, critical_world):
    rid = critical_world["new_report"]()
    r = client.post(f"/api/exams/reports/{rid}/acknowledge", headers=critical_world["doc_b"])
    assert r.status_code == 403, r.text
    assert _state(rid) == ("notified", 0), "被拒的确认不能落库，轨迹也不能多一条"


def test_无关机构不能替别家处置反馈(client, critical_world):
    rid = critical_world["new_report"](status="acknowledged")
    r = client.post(f"/api/exams/reports/{rid}/resolve", json={"note": "乙院写的处置"},
                    headers=critical_world["doc_b"])
    assert r.status_code == 403, r.text
    assert _state(rid) == ("acknowledged", 0), "危急值不能被无关机构的一句话闭环"


def test_申请机构的医师照常闭环且留痕(client, critical_world):
    rid = critical_world["new_report"]()
    before = _logs(critical_world["patient_id"])
    doc_a = critical_world["doc_a"]
    assert client.post(f"/api/exams/reports/{rid}/acknowledge", headers=doc_a).status_code == 200
    r = client.post(f"/api/exams/reports/{rid}/resolve", json={"note": "已静脉补液并复查"}, headers=doc_a)
    assert r.status_code == 200, r.text
    assert _state(rid) == ("resolved", 2)
    assert _logs(critical_world["patient_id"]) == before + 2


def test_先判归属再判状态(client, critical_world):
    rid = critical_world["new_report"](critical=False)
    assert client.post(f"/api/exams/reports/{rid}/acknowledge",
                       headers=critical_world["doc_b"]).status_code == 403, "无关机构拿到 422 等于知道了这不是危急值"
    assert client.post(f"/api/exams/reports/{rid}/acknowledge",
                       headers=critical_world["doc_a"]).status_code == 422


def test_报告号不存在照旧404(client, critical_world):
    for path in ("acknowledge", "resolve"):
        r = client.post(f"/api/exams/reports/987654/{path}", json={"note": "x"}, headers=critical_world["doc_b"])
        assert r.status_code == 404, (path, r.text)
