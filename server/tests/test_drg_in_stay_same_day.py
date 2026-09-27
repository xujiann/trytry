"""DRG 事中预警的住院日同样「当日入当日出计 1 天」（P2-533，第十批「日期与期间边界」扫描 X3-5 / X2-11）。

病案打印写着「住院天数 = 出入院日期差、当日入当日出计 1 天：与居民端「我的住院」、成本核算、运行效率、DRG 同一口径」；
事中预警的基线与在院天数却是裸日期差。一组历史全是当日入出院的，均值 0、永不预警；掺几例当日的，均值被拉低一截——
四例当日、一例 4 天，基线 0.8 天（运行效率算的是 1.6 天），住 2 天的病人被报成超均值 2.5 倍。
"""
from datetime import datetime

import pytest

REF_DAY = "2026-08-20"


@pytest.fixture(scope="module")
def seeded(client, admin):
    from app.database import SessionLocal
    from app.models import Admission

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2533 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2533 内科"}).json()["id"]

    def admit(i, diagnosis, group):
        bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": f"P2533-{i}"}).json()
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P2533 患者{i}", "id_card": f"33010619800202{5330 + i:04d}"}).json()["id"]
        adm = client.post("/api/inpatient/admissions", headers=admin, json={
            "patient_id": patient, "ward_id": ward, "bed_id": bed["id"], "diagnosis_name": diagnosis}).json()
        resp = client.post(f"/api/inpatient/admissions/{adm['id']}/case-summary", headers=admin, json={
            "discharge_diagnosis": diagnosis, "total_cost": 3000, "outcome": "好转"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["drg_code"] == group
        return adm["id"]

    def discharge(aid):
        assert client.post(f"/api/inpatient/admissions/{aid}/discharge", headers=admin).status_code == 200

    # 肺炎组 ES31：四例当日入出院、一例住 4 天；另一例到基准日住了 2 天
    pneumonia = [admit(i, "社区获得性肺炎", "ES31") for i in range(5)]
    for aid in pneumonia:
        discharge(aid)
    pneumonia_staying = admit(5, "社区获得性肺炎", "ES31")
    # 脑血管组 BR23：五例全是当日入出院；另一例到基准日住了 5 天
    stroke = [admit(6 + i, "短暂性脑缺血发作", "BR23") for i in range(5)]
    for aid in stroke:
        discharge(aid)
    stroke_staying = admit(11, "短暂性脑缺血发作", "BR23")
    with SessionLocal() as db:
        for aid in pneumonia[:4] + stroke:
            row = db.get(Admission, aid)
            row.admitted_at, row.discharged_at = datetime(2026, 8, 3, 1), datetime(2026, 8, 3, 7)
        row = db.get(Admission, pneumonia[4])
        row.admitted_at, row.discharged_at = datetime(2026, 8, 5, 1), datetime(2026, 8, 9, 1)
        db.get(Admission, pneumonia_staying).admitted_at = datetime(2026, 8, 18, 1)
        db.get(Admission, stroke_staying).admitted_at = datetime(2026, 8, 15, 1)
        db.commit()
    return {"org": org, "pneumonia": pneumonia_staying, "stroke": stroke_staying}


def test_当日入出院按一天进基线(client, admin, seeded):
    body = client.get("/api/drgs/in-stay-alerts", headers=admin,
                      params={"org_id": seeded["org"], "today": REF_DAY}).json()
    alerts = {a["admission_id"]: a for a in body["alerts"]}
    # 修前：基线 (0+0+0+0+4)/5 = 0.8 天，住 2 天的报成超 2.5 倍；修后基线 1.6 天、2 天不到 1.5 倍
    assert seeded["pneumonia"] not in alerts, alerts
    # 修前：一组全是当日入出院的，均值 0、永不预警；修后基线 1 天，住 5 天的报出来
    stroke = alerts[seeded["stroke"]]
    assert (stroke["stayed_days"], stroke["baseline_avg_days"], stroke["over_ratio"]) == (5, 1.0, 5.0)
