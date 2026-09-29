"""支付单逐笔退款流水（P2-730，第十九批「逆操作是否撤净」扫描 K2-2）。

退款原先只在支付单上累加 `refunded_amount`、覆写 `refunded_at`：退款原因收下就丢，通道的退款单号只在回执里回显一次，
两名经办先后退 30、70，库里只剩累计 100 和第二笔的时间——患者说「只到账 30」时答不出是哪一笔、谁退的、为什么退，
日终对账也没法与通道的退款流水逐笔对上。修法：新表 `payment_refunds`，每退成一笔追加一行（与占额同一次提交）；
`GET /api/billing/payments/{id}/refunds` 逐笔列出，归属按支付单所挂结算单的机构判。通道失败的不记。
"""
import pytest

from conftest import login

from app.database import SessionLocal
from app.models import PaymentRefund
from app.routers.billing import MOCK_GATEWAY


@pytest.fixture(autouse=True)
def clean_gateway():
    MOCK_GATEWAY.reset()
    yield
    MOCK_GATEWAY.reset()


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {}
    for key in ("甲", "乙"):
        orgs[key] = client.post("/api/organizations", headers=admin, json={
            "name": f"P2730 {key}院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    for username, role, org in (("p2730_doc", "doctor", "甲"), ("p2730_op1", "operator", "甲"),
                                ("p2730_op2", "operator", "甲"), ("p2730_other", "operator", "乙")):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pass123456", "full_name": username, "role": role, "org_id": orgs[org]})
        assert created.status_code in (200, 201), created.text
    client.post("/api/billing/charge-items", headers=admin, json={
        "code": "P2730-REG", "name": "P2730 诊查费", "category": "treatment", "price": 100})
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2730 患者", "id_card": "320981199001012730"}).json()["id"]
    doctor, op1 = login(client, "p2730_doc", "pass123456"), login(client, "p2730_op1", "pass123456")
    encounter = client.post("/api/encounters", headers=doctor, json={
        "patient_id": patient, "org_id": orgs["甲"], "diagnosis_name": "感冒"}).json()["id"]
    client.post("/api/billing/details", headers=op1, json={
        "patient_id": patient, "encounter_id": encounter, "item_code": "P2730-REG", "quantity": 1})
    settlement = client.post("/api/billing/settlements", headers=op1, json={
        "bill_type": "outpatient", "encounter_id": encounter, "insurance_pay": 0}).json()["id"]
    order = client.post("/api/billing/payments", headers=op1, json={
        "settlement_id": settlement, "channel": "cash"}).json()
    assert order["status"] == "paid" and order["amount"] == 100.0, order
    return {"order": order["id"]}


def _refund(client, username, order, **body):
    return client.post(f"/api/billing/payments/{order}/refund", headers=login(client, username, "pass123456"),
                       json=body)


def test_两名经办先后部分退款_逐笔留下金额单号原因经办(client, world):
    first = _refund(client, "p2730_op1", world["order"], amount=30, reason="患者取消一项检查")
    assert first.status_code == 200, first.text
    MOCK_GATEWAY.fail_next = True   # 通道明确失败的不记
    assert _refund(client, "p2730_op2", world["order"], amount=10).status_code == 502
    second = _refund(client, "p2730_op2", world["order"], reason="余款退回")
    assert second.status_code == 200 and second.json()["status"] == "refunded", second.text
    rows = client.get(f"/api/billing/payments/{world['order']}/refunds",
                      headers=login(client, "p2730_op1", "pass123456")).json()
    assert [(r["amount"], r["refund_no"], r["reason"], r["operator_name"]) for r in rows] == [
        (70.0, second.json()["refund_no"], "余款退回", "p2730_op2"),
        (30.0, first.json()["refund_no"], "患者取消一项检查", "p2730_op1"),
    ]   # 修前：没有这张表，只剩累计 100 与第二笔的时间
    with SessionLocal() as db:
        assert db.query(PaymentRefund).filter_by(order_id=world["order"]).count() == 2


def test_别家机构看不到这张支付单的退款流水(client, world):
    resp = client.get(f"/api/billing/payments/{world['order']}/refunds",
                      headers=login(client, "p2730_other", "pass123456"))
    assert resp.status_code == 403, resp.text


def test_支付单不存在_404(client, admin):
    assert client.get("/api/billing/payments/999999/refunds", headers=admin).status_code == 404
