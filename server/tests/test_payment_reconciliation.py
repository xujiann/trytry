"""块3：统一支付与日终对账——支付成功/失败、退款校验、三类差异检出与金额合计。"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app
from app.routers.billing import MOCK_GATEWAY

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_gateway():
    """每个用例前复位 Mock 通道开关，避免用例间串扰。"""
    MOCK_GATEWAY.reset()
    yield
    MOCK_GATEWAY.reset()


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def base(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "支付测试医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    for username, role in [("pay_doc", "doctor"), ("pay_op", "operator")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
    doctor = login(client, "pay_doc", "pass123456")
    operator = login(client, "pay_op", "pass123456")
    client.post(
        "/api/billing/charge-items",
        json={"code": "REG-PAY", "name": "门诊诊查费", "category": "treatment", "price": 100},
        headers=admin,
    )
    return {"org": org, "doctor": doctor, "operator": operator}


_counter = {"n": 0}


def new_settlement(client, base, admin, quantity=1, insurance_pay=0.0):
    """建患者 → 就诊 → 计费明细 → 门诊结算，返回结算单。"""
    _counter["n"] += 1
    seq = _counter["n"]
    patient = client.post(
        "/api/patients",
        json={"name": f"支付患者{seq}", "id_card": f"32098119900101{seq:04d}"},
        headers=admin,
    ).json()
    encounter = client.post(
        "/api/encounters",
        json={"patient_id": patient["id"], "org_id": base["org"]["id"], "diagnosis_name": "感冒"},
        headers=base["doctor"],
    ).json()
    client.post(
        "/api/billing/details",
        json={
            "patient_id": patient["id"],
            "encounter_id": encounter["id"],
            "item_code": "REG-PAY",
            "quantity": quantity,
        },
        headers=base["operator"],
    )
    resp = client.post(
        "/api/billing/settlements",
        json={
            "bill_type": "outpatient",
            "encounter_id": encounter["id"],
            "insurance_pay": insurance_pay,
        },
        headers=base["operator"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 支付与退款
# ---------------------------------------------------------------------------


def test_payment_success_defaults_to_self_pay(client, admin, base):
    settlement = new_settlement(client, base, admin, quantity=2, insurance_pay=60)
    assert settlement["self_pay"] == 140.0
    resp = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "online"},
        headers=base["operator"],
    )
    assert resp.status_code == 201, resp.text
    order = resp.json()
    assert order["status"] == "paid" and order["amount"] == 140.0
    assert order["trade_no"].startswith("MOCK") and order["paid_at"]
    assert order["channel_name"] == "线上支付"
    # 医保渠道缺省取医保支付金额
    ins = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "insurance"},
        headers=base["operator"],
    ).json()
    assert ins["amount"] == 60.0 and ins["status"] == "paid"
    listed = client.get(
        f"/api/billing/payments?settlement_id={settlement['id']}", headers=base["operator"]
    ).json()
    assert len(listed) == 2


def test_payment_failure_records_reason(client, admin, base):
    settlement = new_settlement(client, base, admin)
    MOCK_GATEWAY.fail_next = True
    order = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "card"},
        headers=base["operator"],
    ).json()
    assert order["status"] == "failed" and order["trade_no"] == ""
    assert "支付失败" in order["fail_reason"] and order["paid_at"] is None
    # 失败单不可退款
    assert client.post(
        f"/api/billing/payments/{order['id']}/refund", json={}, headers=base["operator"]
    ).status_code == 409
    # 失败金额不占用结算单额度，可重新支付
    retry = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "card"},
        headers=base["operator"],
    ).json()
    assert retry["status"] == "paid"


def test_payment_amount_guards(client, admin, base):
    settlement = new_settlement(client, base, admin)  # 总额 100，自付 100
    assert client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "cash", "amount": 150},
        headers=base["operator"],
    ).status_code == 422
    assert client.post(
        "/api/billing/payments", json={"settlement_id": 999999, "channel": "cash"}, headers=base["operator"]
    ).status_code == 404
    # 分次支付累计不得超总额
    client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "cash", "amount": 60},
        headers=base["operator"],
    )
    over = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "cash", "amount": 60},
        headers=base["operator"],
    )
    assert over.status_code == 422 and "已付" in over.json()["detail"]
    # 医师不可收费
    assert client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "cash", "amount": 10},
        headers=base["doctor"],
    ).status_code == 403


def test_refund_rules(client, admin, base):
    settlement = new_settlement(client, base, admin)
    order = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "online"},
        headers=base["operator"],
    ).json()
    # 超额退款拦截
    over = client.post(
        f"/api/billing/payments/{order['id']}/refund", json={"amount": 120}, headers=base["operator"]
    )
    assert over.status_code == 422 and "可退余额" in over.json()["detail"]
    # 部分退款：状态仍为已支付，累计已退金额
    part = client.post(
        f"/api/billing/payments/{order['id']}/refund",
        json={"amount": 30, "reason": "取消项目"},
        headers=base["operator"],
    ).json()
    assert part["status"] == "paid" and part["refunded_amount"] == 30.0
    assert part["refund_no"] and part["refund_amount"] == 30.0
    # 余额全退 → 状态转已退款，且不可再退
    rest = client.post(
        f"/api/billing/payments/{order['id']}/refund", json={}, headers=base["operator"]
    ).json()
    assert rest["status"] == "refunded" and rest["refunded_amount"] == 100.0
    assert client.post(
        f"/api/billing/payments/{order['id']}/refund", json={"amount": 1}, headers=base["operator"]
    ).status_code == 409
    assert client.post(
        "/api/billing/payments/999999/refund", json={}, headers=base["operator"]
    ).status_code == 404


def test_refund_gateway_failure_returns_502(client, admin, base):
    settlement = new_settlement(client, base, admin)
    order = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "online"},
        headers=base["operator"],
    ).json()
    MOCK_GATEWAY.fail_next = True
    resp = client.post(
        f"/api/billing/payments/{order['id']}/refund", json={}, headers=base["operator"]
    )
    assert resp.status_code == 502
    # 通道失败不改本地状态
    after = client.get(
        f"/api/billing/payments?settlement_id={settlement['id']}", headers=base["operator"]
    ).json()[0]
    assert after["status"] == "paid" and after["refunded_amount"] == 0


# ---------------------------------------------------------------------------
# 日终对账
# ---------------------------------------------------------------------------


def test_reconciliation_all_matched(client, admin, base):
    settlement = new_settlement(client, base, admin)
    client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "online"},
        headers=base["operator"],
    )
    batch = client.post(
        f"/api/billing/reconciliation/run?date={TODAY}", headers=base["operator"]
    ).json()
    assert batch["unmatched"] == 0 and batch["diff_amount"] == 0.0
    assert batch["matched"] == batch["total_orders"] and batch["total_orders"] >= 1
    # 金额合计与本地当日已支付净额一致
    payments = client.get("/api/billing/payments", headers=base["operator"]).json()
    local_total = round(
        sum(p["amount"] - p["refunded_amount"] for p in payments if p["status"] in ("paid", "refunded")), 2
    )
    assert batch["total_amount"] == local_total
    assert client.post(
        "/api/billing/reconciliation/run?date=2024-13-99", headers=base["operator"]
    ).status_code == 422


def test_reconciliation_detects_three_diff_types(client, admin, base):
    settlement = new_settlement(client, base, admin, quantity=3)  # 总额 300
    a = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "online", "amount": 100},
        headers=base["operator"],
    ).json()
    b = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "card", "amount": 200},
        headers=base["operator"],
    ).json()
    # 通道侧：a 缺失、b 金额少 20、另有一笔本地无单的流水
    MOCK_GATEWAY.drop_trade_nos = {a["trade_no"]}
    MOCK_GATEWAY.amount_overrides = {b["trade_no"]: 180.0}
    MOCK_GATEWAY.extra_transactions = [{"trade_no": "MOCKGHOST0001", "amount": 45.5}]
    batch = client.post(
        f"/api/billing/reconciliation/run?date={TODAY}", headers=base["operator"]
    ).json()
    by_type = {}
    for d in batch["diffs"]:
        by_type.setdefault(d["diff_type"], []).append(d)
    assert set(by_type) == {"missing_remote", "amount_mismatch", "missing_local"}
    missing_remote = next(d for d in by_type["missing_remote"] if d["order_id"] == a["id"])
    assert missing_remote["local_amount"] == 100.0 and missing_remote["remote_amount"] == 0.0
    mismatch = next(d for d in by_type["amount_mismatch"] if d["order_id"] == b["id"])
    assert mismatch["local_amount"] == 200.0 and mismatch["remote_amount"] == 180.0
    ghost = by_type["missing_local"][0]
    assert ghost["order_id"] is None and ghost["remote_amount"] == 45.5
    assert ghost["diff_type_name"] == "通道有本地无"
    # 差异金额合计 = 100 + 20 + 45.5
    assert batch["diff_amount"] == 165.5
    assert batch["unmatched"] == len(batch["diffs"])
    assert batch["matched"] + len(by_type["missing_remote"]) + len(by_type["amount_mismatch"]) == batch["total_orders"]


def test_reconciliation_rerun_replaces_batch_and_refund_nets_out(client, admin, base):
    settlement = new_settlement(client, base, admin, quantity=2)
    order = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "online", "amount": 200},
        headers=base["operator"],
    ).json()
    client.post(
        f"/api/billing/payments/{order['id']}/refund", json={"amount": 50}, headers=base["operator"]
    )
    # 部分退款后本地净额 150，Mock 通道同步净额 → 仍匹配
    first = client.post(
        f"/api/billing/reconciliation/run?date={TODAY}", headers=base["operator"]
    ).json()
    assert first["unmatched"] == 0
    batches = client.get(f"/api/billing/reconciliation?date={TODAY}", headers=base["operator"]).json()
    assert len(batches) == 1 and batches[0]["id"] == first["id"]  # 重跑覆盖旧批次
    # 无支付发生的日期：空对账单
    empty = client.post(
        "/api/billing/reconciliation/run?date=2000-01-01", headers=base["operator"]
    ).json()
    assert empty["total_orders"] == 0 and empty["total_amount"] == 0.0 and empty["diffs"] == []
    assert len(client.get("/api/billing/reconciliation", headers=base["operator"]).json()) == 2
