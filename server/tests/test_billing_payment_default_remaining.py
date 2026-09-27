"""统一支付留空收的是这一份还没收的（P2-630，第十三批「拆分之和 vs 总额」扫描 Q3-3）。

金额留空的默认额原先恒取整笔（自付 − 押金冲抵 / 医保支付）：先收了一部分（患者现金付一半、刷卡付一半）或退过一部分
之后留空再收，整笔加已收必超、422「支付金额超出结算单未付余额」，只能手算差额再填。居民端「待支付」
（`self_pay_outstanding`）算的就是还没收的那部分，收费处与它对不上。
"""
import itertools

import pytest

ITEM = "P2630-FEE"
_SEQ = itertools.count(1)


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2630 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    client.post("/api/dictionaries/charge/entries", headers=admin, json={"code": ITEM, "name": "治疗费(P2630)"})
    item = client.post("/api/billing/charge-items", headers=admin,
                       json={"code": ITEM, "name": "治疗费(P2630)", "category": "treatment", "price": 500})
    assert item.status_code in (201, 409), item.text
    return {"org": org}


def _settled(client, admin, world, insurance_pay):
    """门诊：记 2 × 500 = 1000 元 → 结算（医保支付 insurance_pay）。"""
    n = next(_SEQ)
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2630 患者{n}", "id_card": f"33010619800101{n:04d}"}).json()["id"]
    encounter = client.post("/api/encounters", headers=admin, json={
        "patient_id": patient, "org_id": world["org"], "diagnosis_name": "高血压"}).json()["id"]
    assert client.post("/api/billing/details", headers=admin, json={
        "patient_id": patient, "encounter_id": encounter, "item_code": ITEM, "quantity": 2}).status_code == 201
    settled = client.post("/api/billing/settlements", headers=admin, json={
        "bill_type": "outpatient", "encounter_id": encounter, "insurance_pay": insurance_pay})
    assert settled.status_code == 201, settled.text
    return settled.json()


def _pay(client, admin, settlement_id, channel, amount=None):
    body = {"settlement_id": settlement_id, "channel": channel}
    if amount is not None:
        body["amount"] = amount
    return client.post("/api/billing/payments", headers=admin, json=body)


def test_先收一部分_留空收剩下的(client, admin, world):
    s = _settled(client, admin, world, insurance_pay=600)
    assert _pay(client, admin, s["id"], "cash", amount=150).status_code == 201
    rest = _pay(client, admin, s["id"], "card")
    assert (rest.status_code, rest.json().get("amount")) == (201, 250), rest.text   # 修前 422：150 + 400 > 400
    done = _pay(client, admin, s["id"], "cash")
    assert done.status_code == 422 and done.json()["detail"].startswith("支付金额超出结算单未付余额"), done.text


def test_退过一部分_留空补收的是退掉的那部分(client, admin, world):
    s = _settled(client, admin, world, insurance_pay=600)
    paid = _pay(client, admin, s["id"], "cash")
    assert paid.status_code == 201 and paid.json()["amount"] == 400, paid.text
    refund = client.post(f"/api/billing/payments/{paid.json()['id']}/refund", headers=admin,
                         json={"amount": 100, "reason": "多收"})
    assert refund.status_code == 200, refund.text
    again = _pay(client, admin, s["id"], "card")
    assert (again.status_code, again.json().get("amount")) == (201, 100), again.text   # 修前 422：300 + 400 > 400


def test_医保渠道同理_自付不受影响(client, admin, world):
    s = _settled(client, admin, world, insurance_pay=600)
    assert _pay(client, admin, s["id"], "insurance", amount=200).status_code == 201
    fund = _pay(client, admin, s["id"], "insurance")
    assert (fund.status_code, fund.json().get("amount")) == (201, 400), fund.text   # 修前 422：200 + 600 > 600
    cash = _pay(client, admin, s["id"], "cash")
    assert (cash.status_code, cash.json().get("amount")) == (201, 400), cash.text
