"""危急值「超时未确认」从最近一次通知起算（第十五批「源头更正后派生不跟」扫描 S3-2）。

报告修订改判为危急值、或改了危急值报告的结论，闭环状态复位为「已通知」并重新通知申请机构（P2-129）——催办清单却仍按
首次出具时刻计时：出具三小时后改判，改判当场就排在「超时未确认」最前，印的还是原报告时间。修后超时从最近一次通知
（出具或修订复位那条留痕）起算，清单多给一列「最近通知」；存量没有留痕的危急报告照旧按出具时刻。
"""
from datetime import timedelta

import pytest

URL = "/api/exams/critical/unacknowledged"


@pytest.fixture(scope="module")
def setup(client, admin):
    town = client.post("/api/organizations", headers=admin, json={
        "name": "S3-2 卫生院", "org_type": "township", "level": "township"}).json()
    patient = client.post("/api/patients", headers=admin, json={
        "name": "S3-2 患者", "id_card": "331782199003033201", "gender": "男"}).json()
    created = client.post("/api/users", headers=admin, json={
        "username": "s32_doc", "password": "passw0rd1", "full_name": "s32_doc", "role": "doctor", "org_id": town["id"]})
    assert created.status_code in (200, 201), created.text
    token = client.post("/api/auth/login", json={"username": "s32_doc", "password": "passw0rd1"}).json()["access_token"]
    return {"town": town, "patient": patient, "doctor": {"Authorization": f"Bearer {token}"}}


def _report(client, admin, setup, *, critical):
    req = client.post("/api/exams", headers=admin, json={
        "patient_id": setup["patient"]["id"], "from_org_id": setup["town"]["id"],
        "center_type": "lab", "item_code": "K-S32", "item_name": "血钾(S3-2)"}).json()
    client.post(f"/api/exams/{req['id']}/claim", headers=admin)
    resp = client.post(f"/api/exams/{req['id']}/report", headers=admin, json={
        "finding": "血钾 4.1mmol/L", "conclusion": "未见异常", "critical": critical, "reported_by": "检验科"})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _age(report_id, *, reported=None, actions=None):
    """把出具时刻 / 这份报告的全部留痕拨回到 N 分钟前。"""
    from app.clock import now_naive
    from app.database import SessionLocal
    from app.models import CriticalAction, ExamReport

    with SessionLocal() as db:
        if reported is not None:
            db.get(ExamReport, report_id).reported_at = now_naive() - timedelta(minutes=reported)
        if actions is not None:
            for action in db.query(CriticalAction).filter(CriticalAction.report_id == report_id):
                action.created_at = now_naive() - timedelta(minutes=actions)
        db.commit()


def _listed(client, admin, report_id):
    return [r for r in client.get(URL, headers=admin).json() if r["report_id"] == report_id]


def test_出具三小时后改判为危急值_不当场算超时_通知满30分钟才进清单(client, admin, setup):
    report_id = _report(client, admin, setup, critical=False)
    _age(report_id, reported=180)
    amended = client.patch(f"/api/exams/reports/{report_id}", headers=setup["doctor"], json={
        "conclusion": "复核：血钾 7.0mmol/L", "critical": True, "reason": "复核改判"})
    assert amended.status_code == 200 and amended.json()["critical_status"] == "notified", amended.text
    assert _listed(client, admin, report_id) == []            # 修前改判当场就在清单里（按三小时前的出具时刻）
    _age(report_id, actions=31)
    (row,) = _listed(client, admin, report_id)
    assert row["notified_at"] > row["reported_at"]             # 最近通知是修订那一刻，报告时间仍是首次出具


def test_出具即危急的照旧按通知计时(client, admin, setup):
    report_id = _report(client, admin, setup, critical=True)
    assert _listed(client, admin, report_id) == []
    _age(report_id, reported=40, actions=40)
    (row,) = _listed(client, admin, report_id)
    assert row["critical_status"] == "notified"


def test_存量没有留痕的危急报告按出具时刻(client, admin, setup):
    from app.database import SessionLocal
    from app.models import CriticalAction

    report_id = _report(client, admin, setup, critical=True)
    with SessionLocal() as db:
        db.query(CriticalAction).filter(CriticalAction.report_id == report_id).delete()
        db.commit()
    _age(report_id, reported=60)
    (row,) = _listed(client, admin, report_id)
    assert row["notified_at"] == row["reported_at"]
