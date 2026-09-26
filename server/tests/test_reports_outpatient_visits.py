"""运营月报的「门急诊人次」不含住院：办入院建的那条住院就诊记录原先一并数进去（P2-153）。

运营报表 CSV 并排两列「门急诊人次」「住院人次」。办入院会同时建一条 `encounter_type="inpatient"` 的就诊记录
（模型注释：outpatient=门诊, inpatient=住院），导出却把全部就诊记录都算作门急诊——同一批入院在两列里各算一次，
县医院的门急诊人次每月多出整整一个住院量。成本核算的门诊人次早就按类型排除了住院。
"""
import csv
import io

import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2153 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2153 病区"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P2153-1"}).json()["id"]
    first, second = (client.post("/api/patients", headers=admin, json={
        "name": f"P2153 患者{i}", "id_card": f"33010619740404{i:04d}", "gender": "男"}).json()["id"] for i in (1, 2))
    visit = client.post("/api/encounters", headers=admin, json={
        "patient_id": first, "org_id": org, "diagnosis_name": "上呼吸道感染"})
    assert visit.status_code == 201, visit.text
    admitted = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": second, "ward_id": ward, "bed_id": bed, "diagnosis_name": "社区获得性肺炎"})
    assert admitted.status_code == 201, admitted.text
    return {"org": org}


def test_门急诊人次不含住院就诊(client, admin, world):
    resp = client.get("/api/reports/operations/export", headers=admin)
    assert resp.status_code == 200, resp.text
    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    header = rows[0]
    (row,) = [r for r in rows[1:] if r[0] == str(world["org"])]
    visits, admissions = row[header.index("门急诊人次")], row[header.index("住院人次")]
    assert (visits, admissions) == ("1", "1")   # 修前 ("2", "1")：那次入院在两列里各算一次
