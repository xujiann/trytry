"""收款把出院结算时的押金冲抵算进去：原先个人自付被押金冲抵了，收银照默认额还能再收一遍（P1-142）。

住院结算按「冲抵额 = min(押金余额, 个人自付)」自动冲抵，回执给出 payable_after_offset（冲抵后应补缴）。冲抵是押金
流水里的一行，不是支付单；收款两处却只看支付单：
- 默认额取整笔 `self_pay`：押金 5000、自付 1000 全额冲抵、应补缴 0，收银员不填金额点「收费」，照收 1000、paid；
- 「收超了没有」按支付单合计比总额：冲抵的那部分不算已收，最多能多收一笔冲抵额。

修法：自付的默认额取冲抵后应补缴的部分（全额冲抵的 422「押金已冲抵全部个人自付，无需再收」），超收判定与网关回调
入账复核都把冲抵算作已收。
"""
import itertools

import pytest

_BEDS = itertools.count(1)
ITEM = "P1142-FEE"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1142 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P1142 病区"}).json()["id"]
    client.post("/api/dictionaries/charge/entries", headers=admin, json={"code": ITEM, "name": "治疗费(P1142)"})
    item = client.post("/api/billing/charge-items", headers=admin,
                       json={"code": ITEM, "name": "治疗费(P1142)", "category": "treatment", "price": 1000})
    assert item.status_code in (201, 409), item.text
    return {"org": org, "ward": ward}


def _settled(client, admin, world, *, deposit, insurance_pay):
    """在院 → 预交押金 → 记 3000 元 → 出院结算，返回结算回执。"""
    n = next(_BEDS)
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": world["ward"], "bed_no": f"P1142-{n}"}).json()
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P1142 患者{n}", "id_card": f"33010619780808{n:04d}", "gender": "女"}).json()["id"]
    admission = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": world["ward"], "bed_id": bed["id"], "diagnosis_name": "阑尾炎"}).json()["id"]
    if deposit:
        assert client.post("/api/billing/deposits", headers=admin, json={
            "admission_id": admission, "amount": deposit, "method": "cash"}).status_code == 201
    assert client.post("/api/billing/details", headers=admin, json={
        "patient_id": patient, "admission_id": admission, "item_code": ITEM, "quantity": 3}).status_code == 201
    settled = client.post("/api/billing/settlements", headers=admin, json={
        "bill_type": "inpatient", "admission_id": admission, "insurance_pay": insurance_pay})
    assert settled.status_code == 201, settled.text
    return settled.json()


def _pay(client, admin, settlement_id, channel, amount=None):
    body = {"settlement_id": settlement_id, "channel": channel}
    if amount is not None:
        body["amount"] = amount
    return client.post("/api/billing/payments", headers=admin, json=body)


def test_押金全额冲抵了自付_照默认额收费_不再收一遍(client, admin, world):
    s = _settled(client, admin, world, deposit=5000, insurance_pay=2000)
    assert (s["self_pay"], s["deposit_offset"], s["payable_after_offset"]) == (1000, 1000, 0)
    resp = _pay(client, admin, s["id"], "cash")
    assert resp.status_code == 422, resp.text   # 修前 201、paid 1000：一笔自付收了两遍
    assert resp.json()["detail"] == "押金已冲抵全部个人自付，无需再收"


def test_部分冲抵_默认额是应补缴_超收判定把冲抵算作已收(client, admin, world):
    s = _settled(client, admin, world, deposit=300, insurance_pay=2000)
    assert (s["self_pay"], s["deposit_offset"], s["payable_after_offset"]) == (1000, 300, 700)
    cash = _pay(client, admin, s["id"], "cash")
    assert cash.status_code == 201 and cash.json()["amount"] == 700   # 修前 1000
    assert _pay(client, admin, s["id"], "insurance").json()["amount"] == 2000
    extra = _pay(client, admin, s["id"], "cash", amount=1)
    assert extra.status_code == 422, extra.text   # 修前 201：700 + 2000 + 1 ≤ 3000，冲抵的 300 不算已收
    # 金额的写法随库不同（整数金额开发库回 int、生产库回 float，P2-70），只核对说明里带上了冲抵
    assert extra.json()["detail"].startswith("支付金额超出结算单未付余额（总额 3000")
    assert "押金冲抵 300" in extra.json()["detail"]


def test_没交押金的照旧按整笔自付收(client, admin, world):
    s = _settled(client, admin, world, deposit=0, insurance_pay=2000)
    assert (s["self_pay"], s["deposit_offset"], s["payable_after_offset"]) == (1000, 0, 1000)
    assert _pay(client, admin, s["id"], "cash").json()["amount"] == 1000
