"""已办结算的住院不再收费用：结算之后再记一笔，这位患者原先永远出不了院（P1-141）。

计费只挡「已出院」。住院结算发生在出院之前，结算之后、出院之前再记一笔（补一天床位费、一次化验）：
- 出院被「存在未结清住院费用 N 元，结算后方可出院」挡住；
- 再结算撞「一次住院一张结算单」（部分唯一索引）409「不可重复结算」；
- 平台上没有作废明细、没有补结算——患者卡在「在院」，床位一直占着。

修法：已有住院结算单的，计费 409「该次住院已办理结算，不可再计费」（判定与写入圈在结算同一把住院登记行锁里）。
"""
import itertools

import pytest

_BEDS = itertools.count(1)
ITEM = "P1141-BED"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1141 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P1141 病区"}).json()["id"]
    # 收费字典有条目时目录外编码建不了收费项目（别的用例可能已配过字典），先把编码登进字典
    client.post("/api/dictionaries/charge/entries", headers=admin, json={"code": ITEM, "name": "床位费(P1141)"})
    item = client.post("/api/billing/charge-items", headers=admin,
                       json={"code": ITEM, "name": "床位费(P1141)", "category": "bed", "price": 60})
    assert item.status_code in (201, 409), item.text
    return {"org": org, "ward": ward}


def _admit(client, admin, world):
    n = next(_BEDS)
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": world["ward"], "bed_no": f"P1141-{n}"}).json()
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P1141 患者{n}", "id_card": f"33010619770707{n:04d}", "gender": "男"}).json()["id"]
    adm = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": world["ward"], "bed_id": bed["id"], "diagnosis_name": "肺炎"})
    assert adm.status_code == 201, adm.text
    return patient, adm.json()["id"]


def _charge(client, admin, patient, admission, qty=1):
    return client.post("/api/billing/details", headers=admin, json={
        "patient_id": patient, "admission_id": admission, "item_code": ITEM, "quantity": qty})


def _settle(client, admin, admission):
    return client.post("/api/billing/settlements", headers=admin, json={
        "bill_type": "inpatient", "admission_id": admission, "insurance_pay": 0})


def test_结算之后再计费_409_患者照常出院(client, admin, world):
    patient, admission = _admit(client, admin, world)
    assert _charge(client, admin, patient, admission, qty=3).status_code == 201
    assert _settle(client, admin, admission).status_code == 201

    late = _charge(client, admin, patient, admission)
    assert late.status_code == 409, late.text   # 修前 201，之后出院与再结算都 409，永远出不了院
    assert late.json()["detail"] == "该次住院已办理结算，不可再计费"
    details = client.get(f"/api/billing/details?admission_id={admission}", headers=admin).json()
    assert [d["quantity"] for d in details] == [3]   # 那一笔没落库

    client.post(f"/api/inpatient/admissions/{admission}/case-summary", headers=admin, json={
        "discharge_diagnosis": "肺炎治愈", "total_cost": 180, "drug_cost": 0, "outcome": "治愈"})
    discharged = client.post(f"/api/inpatient/admissions/{admission}/discharge", headers=admin)
    assert discharged.status_code == 200, discharged.text


def test_没结算的住院照常计费_门诊计费不受影响(client, admin, world):
    patient, admission = _admit(client, admin, world)
    assert _charge(client, admin, patient, admission).status_code == 201
    assert _charge(client, admin, patient, admission).status_code == 201
    assert len(client.get(f"/api/billing/details?admission_id={admission}", headers=admin).json()) == 2
