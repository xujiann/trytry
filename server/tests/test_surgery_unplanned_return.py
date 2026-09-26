"""手术申请的「非计划重返手术室」勾得上、看得见（P2-172）。

手册写「非计划重返手术室：提手术申请时如实勾选」，质量指标「非计划重返手术室率」数的正是这个勾；申请表单上
原先没有这一项（接口收 `unplanned_return`，页面从不送），指标恒为 0。页面补了勾选框（端到端档驱动），
申请清单的出参带上这个标记，勾没勾在清单上看得见。
"""
import os

import pytest

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


@pytest.fixture(scope="module")
def admission(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2172 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2172 外科"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "1"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2172 患者", "id_card": "330106197207071721"}).json()["id"]
    adm = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "胆囊结石"})
    assert adm.status_code == 201, adm.text
    return adm.json()["id"]


def test_申请清单带出非计划重返标记(client, admin, admission):
    for name, flag in (("腹腔镜胆囊切除术", False), ("胆漏再探查术", True)):
        resp = client.post("/api/surgery/requests", headers=admin, json={
            "admission_id": admission, "surgery_name": name, "unplanned_return": flag})
        assert resp.status_code == 201, resp.text
        assert resp.json()["unplanned_return"] is flag
    rows = client.get(f"/api/surgery/requests?admission_id={admission}", headers=admin).json()
    assert sorted((r["surgery_name"], r["unplanned_return"]) for r in rows) == [
        ("胆漏再探查术", True), ("腹腔镜胆囊切除术", False)]


def test_申请表单有勾选框且送出勾选值():
    with open(os.path.join(STATIC, "pages-mgmt.js"), encoding="utf-8") as fh:
        source = fh.read()
    form = source[source.index('id="surg-form"'):source.index("</form>", source.index('id="surg-form"'))]
    assert 'type="checkbox" name="unplanned_return"' in form   # 修前表单里没有这一项
    assert "body.unplanned_return = e.target.unplanned_return.checked" in source
