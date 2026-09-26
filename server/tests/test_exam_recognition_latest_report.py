"""开单前互认预检的「最近一份」按申请单号取：早开单、晚出报告的那份排在后面，弹出来的是更早的一份结论（P2-272）。

`_find_recognizable` 的 docstring：「30 天窗口内能被这张新申请互认的最近一份同项目报告」——按 `ExamRequest.id` 倒序取。
申请单号是开单的先后，不是出报告的先后：出报告慢的项目、补做的检查，开单在前、报告在后，最新的结论反而排在后面，医生按
预检弹出的「可互认」选了互认，引用的是更早那份过期的结论。修法：按出报告时间倒序（同一时刻再按单号）。
"""
from datetime import timedelta


def test_按出报告时间取最近一份(client, admin):
    from app.clock import now_naive
    from app.database import SessionLocal
    from app.models import ExamReport

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2272 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2272 患者", "id_card": "330106197708082272"}).json()["id"]

    def reported(conclusion):
        req = client.post("/api/exams", headers=admin, json={
            "patient_id": patient, "from_org_id": org, "center_type": "imaging",
            "item_code": "P2272CT", "item_name": "胸部CT"}).json()
        report = client.post(f"/api/exams/{req['id']}/report", headers=admin,
                             json={"conclusion": conclusion, "critical": False})
        assert report.status_code == 201, report.text
        return req["id"], report.json()["id"]

    early_req, early_report = reported("早开单、晚出报告：最新结论")
    late_req, late_report = reported("晚开单、早出报告：旧结论")
    with SessionLocal() as db:   # 晚开单的那份报告其实出得更早（十天前）
        db.get(ExamReport, late_report).reported_at = now_naive() - timedelta(days=10)
        db.get(ExamReport, early_report).reported_at = now_naive() - timedelta(hours=1)
        db.commit()
    check = client.get("/api/exams/recognition-check", headers=admin,
                       params={"patient_id": patient, "item_code": "P2272CT", "center_type": "imaging"}).json()
    assert check["recognizable"] is True
    assert check["request_id"] == early_req, check   # 修前是 late_req：按申请单号取了更早的结论
    assert check["conclusion"] == "早开单、晚出报告：最新结论"
