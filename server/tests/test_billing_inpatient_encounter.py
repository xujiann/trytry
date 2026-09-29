"""住院类就诊不按门诊计费、不按门诊结算，门诊药占比也不算它（P2-911，第二十五批「费用与业务状态」扫描 J3-2）。

入院登记会建一条 `encounter_type="inpatient"` 的就诊（FHIR 按 IMP 入站的也是）。计费与门诊结算的就诊分支只查就诊
存在、患者一致、机构权限：住院结算之后按住院号再计费被拒（P1-141），改填这次住院的就诊号就 201；带着这笔没结的明细
照样出院，之后还能按门诊结算掉——结算统计多一笔门诊、门诊药占比变 100%，居民端住院费用清单里却没有它。明细的规矩是
「门诊按就诊、住院按住院登记」。修后两处都 422；门诊药占比与本文件门诊人次同一句，排除住院类就诊。
"""
import pytest

from conftest import business_today

from app.database import SessionLocal
from app.models import BillDetail, Encounter, User

ITEM = "P2911-DRUG"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2911 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2911 病区"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P2911-1"}).json()["id"]
    client.post("/api/dictionaries/charge/entries", headers=admin, json={"code": ITEM, "name": "药品(P2911)"})
    item = client.post("/api/billing/charge-items", headers=admin,
                       json={"code": ITEM, "name": "药品(P2911)", "category": "drug", "price": 600})
    assert item.status_code in (201, 409), item.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2911 患者", "id_card": "330106197707072911", "gender": "男"}).json()["id"]
    adm = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "肺炎"})
    assert adm.status_code == 201, adm.text
    with SessionLocal() as db:
        encounter = db.query(Encounter.id).filter(Encounter.patient_id == patient,
                                                  Encounter.encounter_type == "inpatient").scalar()
    return {"org": org, "patient": patient, "admission": adm.json()["id"], "encounter": encounter}


def test_住院类就诊_按门诊计费与结算都422(client, admin, world):
    got = client.post("/api/billing/details", headers=admin, json={
        "patient_id": world["patient"], "encounter_id": world["encounter"], "item_code": ITEM, "quantity": 1})
    assert got.status_code == 422, got.text   # 修前 201：绕过 P1-141，住院结算与出院门禁都看不见它
    assert "请按住院号" in got.json()["detail"]
    with SessionLocal() as db:   # 修前落下的存量明细
        operator = db.query(User.id).filter(User.username == "admin").scalar()
        db.add(BillDetail(patient_id=world["patient"], encounter_id=world["encounter"], item_code=ITEM,
                          item_name="药品(P2911)", unit_price=600, quantity=1, amount=600, created_by=operator))
        db.commit()
    got = client.post("/api/billing/settlements", headers=admin, json={
        "bill_type": "outpatient", "encounter_id": world["encounter"], "insurance_pay": 0})
    assert got.status_code == 422, got.text   # 修前 201：按门诊结算掉、计进门诊统计
    assert "不能按门诊结算" in got.json()["detail"]


def test_门诊药占比不算住院类就诊的明细(client, admin, world):
    rows = client.get("/api/analytics/drug-use", headers=admin,
                      params={"period": business_today().strftime("%Y-%m"), "org_id": world["org"]}).json()
    row = next(r for r in rows["orgs"] if r["org_id"] == world["org"])
    assert row["outpatient_drug_ratio_pct"] in (0, None), row   # 修前 100.0：住院就诊号上的药费算成门诊
