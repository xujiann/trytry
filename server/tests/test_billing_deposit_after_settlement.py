"""已办结算的住院不再收押金（P2-912，第二十五批「费用与业务状态」扫描 J3-3）。

`create_deposit` 只判住院状态是不是「在院」。押金只在建结算单那一刻冲抵一次，结算之后预交的钱没有任何去处：结算后
自付 1000、冲抵 0 → 预交 1000 成功 → 居民端仍显示待支付 1000 → 收银按默认额（自付 − 冲抵）又收了 1000 → 出院，押金
1000 原样挂着，患者共付出 2000。兄弟路径计费早已「该次住院已办理结算，不可再计费」（P1-141）。修后同一句 409；结算前
预交照常。
"""
import pytest

ITEM = "P2912-BED"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2912 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2912 病区"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P2912-1"}).json()["id"]
    client.post("/api/dictionaries/charge/entries", headers=admin, json={"code": ITEM, "name": "床位费(P2912)"})
    item = client.post("/api/billing/charge-items", headers=admin,
                       json={"code": ITEM, "name": "床位费(P2912)", "category": "bed", "price": 1000})
    assert item.status_code in (201, 409), item.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2912 患者", "id_card": "330106197808082912", "gender": "男"}).json()["id"]
    adm = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "肺炎"})
    assert adm.status_code == 201, adm.text
    return {"patient": patient, "admission": adm.json()["id"]}


def _deposit(client, admin, admission, amount):
    return client.post("/api/billing/deposits", headers=admin, json={
        "admission_id": admission, "amount": amount, "method": "cash"})


def test_结算前预交照常_结算后409(client, admin, world):
    admission = world["admission"]
    assert _deposit(client, admin, admission, 200).status_code == 201   # 结算前照常
    assert client.post("/api/billing/details", headers=admin, json={
        "patient_id": world["patient"], "admission_id": admission, "item_code": ITEM, "quantity": 1}).status_code == 201
    settled = client.post("/api/billing/settlements", headers=admin, json={
        "bill_type": "inpatient", "admission_id": admission, "insurance_pay": 0})
    assert settled.status_code == 201, settled.text
    got = _deposit(client, admin, admission, 1000)
    assert got.status_code == 409, got.text   # 修前 201：冲不到结算单上，收银按默认额再收一遍
    assert "统一支付" in got.json()["detail"]
