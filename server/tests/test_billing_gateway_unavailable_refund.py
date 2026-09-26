"""网关没注册上时，退款与对账悄悄落回模拟通道：网关单退款 200「已退款」、患者一分钱没拿到，对账「差异 0 笔」（P1-165）。

`_gateway(channel)` 对没注册的渠道一律落回 Mock，Mock 的 refund 直接回成功、查流水就是把本地单照抄一遍。下单那一处早就
挡了（「未配置/未过出网校验时绝不能悄悄落回 Mock：Mock 会把单标成已支付，而现实中一分钱都没收到」），退款与对账没挡：
网关地址出网校验不过、或启动那一刻 DNS 解析失败，网关就没注册上——之前经网关收的单这时点退款，200「已退款」、流水号
「RF…」，通道一次也没调，患者没拿到钱，这张单的额度却释放了、还能再收一遍；当日对账拿本地镜像比本地单，一笔不差。

修法：`_needs_real_gateway` 一句话，下单、退款、对账同用——`gateway` 渠道一律、`online` 在生产环境要真通道，没有就 503；
现金、银行卡、医保本来就没有网关，照旧。
"""
import itertools

import pytest

from app.config import settings
from app.routers import billing

ITEM = "P1165-FEE"
_SEQ = itertools.count(1)


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1165 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    client.post("/api/dictionaries/charge/entries", headers=admin, json={"code": ITEM, "name": "诊查费(P1165)"})
    item = client.post("/api/billing/charge-items", headers=admin,
                       json={"code": ITEM, "name": "诊查费(P1165)", "category": "treatment", "price": 100})
    assert item.status_code in (201, 409), item.text
    return {"org": org}


@pytest.fixture(autouse=True)
def no_gateway():
    billing._GATEWAYS.pop("gateway", None)   # 网关没注册上（出网校验不过 / 启动时 DNS 解析失败）
    billing._GATEWAYS.pop("online", None)


def _paid_order(client, admin, world, channel):
    """一张已结算的门诊单，挂一张已支付的 channel 渠道支付单（网关单是网关还在时收的），返回支付单。"""
    from app.database import SessionLocal
    from app.models import PaymentOrder, User
    from app.models._base import utcnow

    n = next(_SEQ)
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P1165 患者{n}", "id_card": f"33010619811111{n:04d}"}).json()["id"]
    encounter = client.post("/api/encounters", headers=admin, json={
        "patient_id": patient, "org_id": world["org"], "diagnosis_name": "复诊"}).json()["id"]
    assert client.post("/api/billing/details", headers=admin, json={
        "patient_id": patient, "encounter_id": encounter, "item_code": ITEM}).status_code == 201
    settled = client.post("/api/billing/settlements", headers=admin, json={
        "bill_type": "outpatient", "encounter_id": encounter, "insurance_pay": 0})
    assert settled.status_code == 201, settled.text
    with SessionLocal() as db:
        creator = db.query(User).filter_by(username="admin").one().id
        order = PaymentOrder(settlement_id=settled.json()["id"], channel=channel, amount=100, status="paid",
                             trade_no=f"P1165-{channel}-{n}", paid_at=utcnow(), created_by=creator)
        db.add(order)
        db.commit()
        return {"id": order.id, "day": order.paid_at.strftime("%Y-%m-%d")}


def _order(order_id):
    from app.database import SessionLocal
    from app.models import PaymentOrder

    with SessionLocal() as db:
        o = db.get(PaymentOrder, order_id)
        return o.status, o.refunded_amount


def test_网关没注册上_网关单退款503_单据不动(client, admin, world):
    order = _paid_order(client, admin, world, "gateway")
    resp = client.post(f"/api/billing/payments/{order['id']}/refund", headers=admin, json={})
    assert resp.status_code == 503, resp.text   # 修前 200「已退款」：通道一次没调，患者没拿到钱
    assert "暂不能退款" in resp.json()["detail"]
    assert _order(order["id"]) == ("paid", 0)


def test_网关没注册上_当日有网关单的对账503(client, admin, world):
    order = _paid_order(client, admin, world, "gateway")
    resp = client.post(f"/api/billing/reconciliation/run?date={order['day']}", headers=admin)
    assert resp.status_code == 503, resp.text   # 修前 201，拿本地镜像比本地单：「差异 0 笔」
    assert "暂不能对账" in resp.json()["detail"]


def test_生产环境线上支付没有真通道_退款503(client, admin, world, monkeypatch):
    order = _paid_order(client, admin, world, "online")
    monkeypatch.setattr(settings, "env", "prod")
    resp = client.post(f"/api/billing/payments/{order['id']}/refund", headers=admin, json={})
    assert resp.status_code == 503, resp.text
    assert _order(order["id"]) == ("paid", 0)


def test_现金单照常退款(client, admin, world, monkeypatch):
    order = _paid_order(client, admin, world, "cash")
    monkeypatch.setattr(settings, "env", "prod")
    resp = client.post(f"/api/billing/payments/{order['id']}/refund", headers=admin, json={})
    assert resp.status_code == 200 and resp.json()["status"] == "refunded", resp.text
