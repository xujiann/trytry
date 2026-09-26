"""网页建档的患者也能命中审方特殊人群（P1-153）。

审方的儿童 / 老年按出生日期现算年龄，孕产妇按在册孕产档案 + 性别「女」认。网页上唯一的建档表单只送姓名、身份证号、
性别、电话——从不送出生日期，性别又缺省「未知」；孕产妇建册也不要求性别是女。于是网页建档的 78 岁老人开华法林、
6 岁孩子开左氧氟沙星、性别没改过的在册孕妇开利伐沙班，全都系统审通过；同样的人经 HL7 带着出生日期进来就转药师审。
修法：建档表单加出生日期；孕产妇只排除明确登记为「男」的——在册的孕产档案本身就是证据。
"""
import itertools
import os

import pytest

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
_CARDS = itertools.count(1)


@pytest.fixture(scope="module")
def org(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1153 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    for code, group in (("P1153-RIVA", "pregnant"), ("P1153-WARF", "elderly")):
        resp = client.post("/api/prescriptions/rules", headers=admin, json={
            "drug_code": code, "max_daily_dose": 80, "special_groups": group})
        assert resp.status_code == 201, resp.text
    return org


def _patient(client, admin, **extra):
    resp = client.post("/api/patients", headers=admin, json={
        "name": "P1153 患者", "id_card": f"33010619900101{next(_CARDS):04d}", **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _prescribe(client, admin, org, patient_id, code):
    resp = client.post("/api/prescriptions", headers=admin, json={
        "patient_id": patient_id, "org_id": org,
        "items": [{"drug_code": code, "drug_name": code, "daily_dose": 10}]})
    assert resp.status_code == 201, resp.text
    return resp.json()["status"]


def test_性别未知的在册孕妇_开孕产妇慎用药转药师审(client, admin, org):
    patient = _patient(client, admin)                                    # 性别缺省「未知」，与网页建档一样
    assert client.post("/api/maternal/records", headers=admin, json={"patient_id": patient}).status_code == 201
    assert _prescribe(client, admin, org, patient, "P1153-RIVA") == "pending_review"   # 修前 auto_passed


def test_明确登记为男的_有孕产档案也不算孕产妇(client, admin, org):
    patient = _patient(client, admin, gender="男")
    assert client.post("/api/maternal/records", headers=admin, json={"patient_id": patient}).status_code == 201
    assert _prescribe(client, admin, org, patient, "P1153-RIVA") == "auto_passed"


def test_带出生日期建档的老人_开老年慎用药转药师审(client, admin, org):
    patient = _patient(client, admin, gender="男", birth_date="1948-03-01")
    assert _prescribe(client, admin, org, patient, "P1153-WARF") == "pending_review"


def test_网页建档表单送出生日期():
    with open(os.path.join(STATIC, "core.js"), encoding="utf-8") as fh:
        source = fh.read()
    start = source.index('id="patient-form"')
    form = source[start:source.index("</form>", start)]
    assert 'name="birth_date" type="date"' in form                        # 修前表单没有这一格
    handler = source[source.index('$("#patient-form").onsubmit'):]
    handler = handler[:handler.index('$("#patient-search").onsubmit')]
    assert '...(f.get("birth_date") ? { birth_date: f.get("birth_date") } : {})' in handler
