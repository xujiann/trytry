"""危急值清单按报告号倒序取最新 100 条、各种状态混排：已确认还没反馈的那条被新出的挤出窗口，「处置反馈」再也点不到（P1-166）。

`GET /api/exams/critical` 是「确认接收」「处置反馈」两个按钮唯一的所在（管理端危急值页、医生移动端危急值页都取它）。
全县一天几十条危急值：前天已确认、还没反馈的那条被之后出的 100 条（大多已处置）挤出窗口；「超时未确认催办」那张表只收
未确认的，也看不到它——这条危急值永远闭不了环（与 P1-148 审方队列同一个形状）。

修法：没处置完的排在最前，之后才是最近已处置的。
"""
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def test_已确认未反馈的危急值不被新出的已处置挤出清单(client, admin):
    from app.database import SessionLocal
    from app.models import ExamReport, ExamRequest, User
    from app.routers.exams import CRITICAL_LIST_LIMIT

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1166 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P1166 患者", "id_card": "330127196505051166"}).json()["id"]

    def _critical(db, status):
        creator = db.query(User).filter_by(username="admin").one().id
        req = ExamRequest(patient_id=patient, from_org_id=org, center_type="lab", item_code="K",
                          item_name="血钾", status="reported", created_by=creator)
        db.add(req)
        db.flush()
        report = ExamReport(request_id=req.id, conclusion="危急值：血钾 7.1", critical=True, critical_status=status)
        db.add(report)
        db.flush()
        return report.id

    with SessionLocal() as db:
        open_id = _critical(db, "acknowledged")   # 前天出的、已确认、还没处置反馈
        for _ in range(CRITICAL_LIST_LIMIT):       # 之后又出了一百条，都已处置完
            _critical(db, "resolved")
        db.commit()
    rows = client.get("/api/exams/critical", headers=admin).json()
    assert len(rows) == CRITICAL_LIST_LIMIT
    ids = [r["id"] for r in rows]
    assert open_id in ids                         # 修前被挤出窗口，「处置反馈」再也点不到
    open_rows = [r for r in rows if r["critical_status"] != "resolved"]
    assert rows[: len(open_rows)] == open_rows    # 没处置完的都排在最前


def test_两个危急值页都从这张清单取按钮():
    assert 'api("/api/exams/critical")' in (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    assert 'api("/api/exams/critical")' in (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")
