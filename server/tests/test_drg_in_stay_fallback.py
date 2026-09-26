"""DRG 事中预警不拿兜底组 QY 当「同组」：QY 不建基线、在院的 QY 病例不预警、计入未入组（P2-166）。

事中预警的基线说「取本院已出院且已入组的历史病例」「同组均值」；/stats 的口径是「grouped 仅统计正式分组，
QY 兜底组……从 CMI 分母剔除」。实现却把 QY 也建了基线：哪组都没匹配上的病例凑成一个「均值」，在院的 QY 病例
拿它预警——预警的是凑数的均值；它们也不进「未入组的在院病例」（页面写着「没有 DRG 就没有同组均值可比，不参与预警」）。
"""
from datetime import datetime

import pytest

REF_DAY = "2026-08-20"


@pytest.fixture(scope="module")
def seeded(client, admin):
    from app.database import SessionLocal
    from app.models import Admission

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2166 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2166 内科"}).json()["id"]
    beds = [client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": f"P2166-{i}"}).json()["id"]
            for i in range(6)]

    def admit(i):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P2166 患者{i}", "id_card": f"33010619700101{i:04d}"}).json()["id"]
        adm = client.post("/api/inpatient/admissions", headers=admin, json={
            "patient_id": patient, "ward_id": ward, "bed_id": beds[i], "diagnosis_name": "罕见代谢病"}).json()
        resp = client.post(f"/api/inpatient/admissions/{adm['id']}/case-summary", headers=admin, json={
            "discharge_diagnosis": "罕见代谢病", "total_cost": 3000, "outcome": "好转"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["drg_code"] == "QY"   # 哪组都没匹配上，落兜底组
        return adm["id"]

    history = []
    for i in range(5):   # 五例 QY 病例住 2 天出院——凑得出一个「同组均值」
        aid = admit(i)
        assert client.post(f"/api/inpatient/admissions/{aid}/discharge", headers=admin).status_code == 200
        history.append(aid)
    staying = admit(5)
    with SessionLocal() as db:
        for aid in history:
            row = db.get(Admission, aid)
            row.admitted_at, row.discharged_at = datetime(2026, 8, 1, 8), datetime(2026, 8, 3, 8)
        db.get(Admission, staying).admitted_at = datetime(2026, 8, 10, 8)   # 到基准日已住 10 天
        db.commit()
    return {"org": org, "staying": staying}


def test_在院的兜底组病例不拿兜底组均值预警_计入未入组(client, admin, seeded):
    body = client.get("/api/drgs/in-stay-alerts", headers=admin,
                      params={"org_id": seeded["org"], "today": REF_DAY}).json()
    assert body["alerts"] == []   # 修前：住 10 天 > 「均值」2 天 × 1.5，报了预警
    assert body["insufficient_baseline"] == []
    assert body["ungrouped_in_stay"] == 1   # 修前 0
