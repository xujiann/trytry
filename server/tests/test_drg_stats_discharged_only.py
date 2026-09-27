"""DRG 统计把在院患者算成「出院病例」：例数、入组率、CMI、均次费用都掺着还没出院的（P2-453）。

页面列名是「出院病例」，模块口径是「出院病例入组」，事中预警的历史基线也只取已出院的；可病案首页出院前就得填（不填
不让出院），在院患者早有分组与费用，`/api/drgs/stats` 原先照样算进去：两例都还在院，机构行照样报 2 例、CMI 0.95、
均次 5500。在院的费用还没结完，是事中预警的对象，不是出院病例。修后：只算已出院的。
"""


def _admit_with_summary(client, admin, org, ward, n, cost):
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": f"P2453-{n}"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2453 患者{n}", "id_card": f"33010619610101{2450 + n:04d}"}).json()["id"]
    adm = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "社区获得性肺炎"}).json()["id"]
    summary = client.post(f"/api/inpatient/admissions/{adm}/case-summary", headers=admin, json={
        "discharge_diagnosis": "社区获得性肺炎", "total_cost": cost, "outcome": "好转"})
    assert summary.status_code == 201, summary.text
    return adm


def _org_row(client, admin, org):
    body = client.get("/api/drgs/stats", headers=admin).json()
    return next((o for o in body["orgs"] if o["org_id"] == org), None)


def test_在院的首页已入组_不算出院病例_出院之后才算(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2453 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2453 呼吸科"}).json()["id"]
    first = _admit_with_summary(client, admin, org, ward, 1, 5000)
    _admit_with_summary(client, admin, org, ward, 2, 6000)
    assert _org_row(client, admin, org) is None   # 修前：两例都在院，照样报 2 例、CMI 0.95、均次 5500
    assert client.post(f"/api/inpatient/admissions/{first}/discharge", headers=admin).status_code == 200
    row = _org_row(client, admin, org)
    assert (row["cases"], row["grouped"], row["avg_cost"]) == (1, 1, 5000.0)
