"""收费结算的金额并发闸门：押金 / 结算 / 缴费 / 退款 / 网关占额。

**这一套必须在真 PostgreSQL 上跑。** 其中三条（押金退费、出院结算、并发缴费）
在 SQLite 上根本复现不出来——SQLite 是库级写锁，把"判定"和"写入"之间的窗口
一并锁掉了；PG 的 READ COMMITTED 逐语句取快照，并发事务互不可见，窗口是真实
存在的。实测差别（修复前，同一份代码）：

| 场景 | SQLite | PostgreSQL |
|---|---|---|
| 预交 1000、八路各退 200 | 5 笔成功，余额 0 | **8 笔全过，余额 -600** |
| 四路并发出院结算 | 1 张单 | **4 张单 + 4 条医保结算 + 押金多冲 1500** |
| 1000 元账单五路各缴 1000 | 5 张 paid 收 5000 | 5 张 paid 收 5000 |

跑 PG 的接法：`tests/test_postgres_real.py` 里有一条 integration 用例，把本文件
用 `MEDPLAT_BILLING_PG_URL` 指向 PG 再跑一遍（`make test-integration` 的路径）。
默认（不带那个环境变量）就是普通的 SQLite 单元用例，跟着 `make test-unit` 跑。

写法上照抄 `test_stage14_concurrency.py::_race`：**测并发的用例自己必须先真的
并发**——只起 N 个线程是不够的，线程创建本身有先后，前一个常常已经提交完了
后一个才开始读，竞态窗口根本没打开。用 `Barrier` 卡住、放行时才一起进。
"""
import os
import threading

# 引擎是模块级的，`app.database` 一旦导入就定型——切库必须赶在导入之前。
# 只有 test_postgres_real.py 起的那个子进程会带上这个变量。
_PG_URL = os.environ.get("MEDPLAT_BILLING_PG_URL", "")
if _PG_URL:
    os.environ["MEDPLAT_DATABASE_URL"] = _PG_URL

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from conftest import reset_database  # noqa: E402

from app.database import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Deposit, InsuranceSettlement, PaymentOrder, Settlement  # noqa: E402
from app.routers import billing  # noqa: E402

if _PG_URL:
    assert engine.dialect.name == "postgresql", (
        "MEDPLAT_BILLING_PG_URL 已给出，引擎却不是 PostgreSQL——"
        f"实际 {engine.dialect.name}，多半是 app.database 在本模块之前就被导入了"
    )


def _race(call, times=8):
    """并发跑同一个请求，返回按可排序键排好的结果列表。

    与 `test_stage14_concurrency.py::_race` 同一个形状，只是这里要看的不只是
    状态码，还有金额，所以 `call` 返回什么就收什么。
    """
    results: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(times)

    def run(index: int):
        barrier.wait(timeout=30)
        outcome = call(index)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(times)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sorted(results, key=str)


@pytest.fixture(scope="module")
def client():
    reset_database()
    # raise_server_exceptions=False：并发下要断言的正是"会不会出 500"，
    # 让异常抛进用例就看不到状态码了。
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "金额并发县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def ward(client, admin, org):
    return client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "并发内科"}, headers=admin
    ).json()


_seq = {"n": 0}


def _next_id_card() -> str:
    _seq["n"] += 1
    return f"32000019900101{_seq['n']:04d}"


def _patient(client, admin, name):
    return client.post(
        "/api/patients", json={"name": name, "id_card": _next_id_card()}, headers=admin
    ).json()


def _admission(client, admin, ward, name):
    patient = _patient(client, admin, name)
    _seq["n"] += 1
    bed = client.post(
        "/api/inpatient/beds",
        json={"ward_id": ward["id"], "bed_no": f"B{_seq['n']:03d}"},
        headers=admin,
    ).json()
    admission = client.post(
        "/api/inpatient/admissions",
        json={
            "patient_id": patient["id"],
            "ward_id": ward["id"],
            "bed_id": bed["id"],
            "doctor_name": "并发医生",
            "diagnosis_name": "肺炎",
        },
        headers=admin,
    ).json()
    return patient, admission


def _charge_item(client, admin, code, price):
    client.post(
        "/api/billing/charge-items",
        json={"code": code, "name": f"项目{code}", "category": "other", "price": price},
        headers=admin,
    )
    return code


def _encounter_settlement(client, admin, org, name, code, quantity):
    """建一张门诊结算单，返回 (结算单, 就诊)。"""
    patient = _patient(client, admin, name)
    encounter = client.post(
        "/api/encounters",
        json={
            "patient_id": patient["id"],
            "org_id": org["id"],
            "encounter_type": "outpatient",
        },
        headers=admin,
    ).json()
    client.post(
        "/api/billing/details",
        json={
            "patient_id": patient["id"],
            "encounter_id": encounter["id"],
            "item_code": code,
            "quantity": quantity,
        },
        headers=admin,
    )
    settlement = client.post(
        "/api/billing/settlements",
        json={"bill_type": "outpatient", "encounter_id": encounter["id"]},
        headers=admin,
    ).json()
    return settlement, encounter


# ---------- B-1 押金 ----------


def test_并发退押金不得把余额退成负数(client, admin, ward):
    """预交 1000、八路并发各退 200：最多 5 笔，余额落到 0，绝不为负。

    改回旧写法（去掉 `_serialized_on`，只留 INSERT..SELECT）时，PG 上八笔全过、
    refunded=1600、balance=-600——押金台账凭空多退出 600 元。
    """
    _, admission = _admission(client, admin, ward, "押金并发患者")
    assert (
        client.post(
            "/api/billing/deposits",
            json={"admission_id": admission["id"], "amount": 1000},
            headers=admin,
        ).status_code
        == 201
    )

    def refund(_i):
        resp = client.post(
            "/api/billing/deposits/refund",
            json={"admission_id": admission["id"], "amount": 200},
            headers=admin,
        )
        return resp.status_code

    codes = _race(refund, times=8)
    assert codes.count(201) == 5, f"余额 1000 只够退 5 笔 200，实际成功 {codes.count(201)} 笔：{codes}"
    assert codes.count(422) == 3, codes
    assert 500 not in codes, f"并发退费不该抛 500：{codes}"

    balance = client.get(
        "/api/billing/deposits/balance",
        params={"admission_id": admission["id"]},
        headers=admin,
    ).json()
    assert balance["refunded"] == 1000.0, f"退费总额只能是 1000，实际 {balance}"
    assert balance["balance"] == 0.0, f"余额不得为负，实际 {balance}"


# ---------- B-3 出院结算 ----------


def test_并发出院结算只出一张结算单与一条医保结算记录(client, admin, ward):
    """四路并发结算同一次住院：一张结算单、一条医保结算、押金只冲抵一次。

    改回旧写法（先查明细、再建单、再逐条回填 settlement_id）时，PG 上四路全部
    201：四张结算单、四条 InsuranceSettlement（医保基金支出重复计入四遍）、
    押金被冲抵 2000（应为 500），且前三张单挂着金额却一条明细都没有。
    """
    patient, admission = _admission(client, admin, ward, "重复结算患者")
    code = _charge_item(client, admin, "RACE-SETTLE", 100)
    client.post(
        "/api/billing/deposits",
        json={"admission_id": admission["id"], "amount": 2000},
        headers=admin,
    )
    client.post(
        "/api/billing/details",
        json={
            "patient_id": patient["id"],
            "admission_id": admission["id"],
            "item_code": code,
            "quantity": 6,
        },
        headers=admin,
    )

    def settle(_i):
        resp = client.post(
            "/api/billing/settlements",
            json={
                "bill_type": "inpatient",
                "admission_id": admission["id"],
                "insurance_pay": 100,
            },
            headers=admin,
        )
        return resp.status_code

    codes = _race(settle, times=4)
    assert codes.count(201) == 1, f"同一次住院只该结算成功一次：{codes}"
    assert 500 not in codes, f"抢输的那几路应是 4xx 而不是 500：{codes}"

    with SessionLocal() as db:
        settlements = (
            db.query(Settlement).filter(Settlement.admission_id == admission["id"]).all()
        )
        assert len(settlements) == 1, f"住院结算单不得重复，实际 {len(settlements)} 张"
        ins_rows = (
            db.query(InsuranceSettlement)
            .filter(InsuranceSettlement.patient_id == patient["id"])
            .all()
        )
        assert len(ins_rows) == 1, (
            f"医保结算记录重复就是基金支出重复计入，实际 {len(ins_rows)} 条"
        )
        offset = (
            db.query(Deposit)
            .filter(
                Deposit.admission_id == admission["id"], Deposit.deposit_type == "offset"
            )
            .all()
        )
        assert len(offset) == 1 and float(offset[0].amount) == 500.0, (
            f"押金冲抵只该发生一次、金额=自付 500，实际 {[float(o.amount) for o in offset]}"
        )

    details = client.get(
        "/api/billing/details",
        params={"admission_id": admission["id"], "settled": "true"},
        headers=admin,
    ).json()
    assert details and all(d["settlement_id"] == settlements[0].id for d in details), (
        "明细必须全部归属那张唯一的结算单"
    )


# ---------- B-5 / 并发缴费 ----------


def test_并发缴费不得收超结算单总额(client, admin, org, monkeypatch):
    """100 元账单五路并发各缴 100：只收 1 笔。

    改回旧写法（先算 paid_already 再落单，中间隔着通道调用）时，五路全部 201，
    一张 100 元的账单收进 500。

    通道调用故意慢 0.3 秒：真实通道的 RTT 就是 check→落单之间那个窗口，
    Mock 通道快到几乎没有窗口，不撑开的话旧代码也能侥幸全绿——
    **测并发的用例要负责把窗口打开**。
    """
    import time

    original_pay = billing.MOCK_GATEWAY.pay

    def slow_pay(order_id, amount, channel):
        time.sleep(0.3)
        return original_pay(order_id, amount, channel)

    monkeypatch.setattr(billing.MOCK_GATEWAY, "pay", slow_pay)
    code = _charge_item(client, admin, "RACE-PAY", 100)
    settlement, _ = _encounter_settlement(client, admin, org, "并发缴费患者", code, 1)

    def pay(_i):
        resp = client.post(
            "/api/billing/payments",
            json={"settlement_id": settlement["id"], "channel": "cash", "amount": 100},
            headers=admin,
        )
        return resp.status_code

    codes = _race(pay, times=5)
    assert codes.count(201) == 1, f"100 元账单只能收一笔 100：{codes}"
    assert 500 not in codes, f"并发缴费不该抛 500：{codes}"

    orders = client.get(
        "/api/billing/payments", params={"settlement_id": settlement["id"]}, headers=admin
    ).json()
    paid = round(sum(o["amount"] for o in orders if o["status"] == "paid"), 2)
    assert paid == 100.0, f"已收合计不得超过账单总额，实际收了 {paid}"


def test_网关pending单占额_同一张账单不会被扫码收两次(client, admin, org, monkeypatch):
    """gateway 渠道下单停在 pending，额度必须当场占住。

    改回旧写法（paid_already 只统计 paid|refunded）时，同一张 100 元账单可以
    扫三次码开出三张 pending 单，三次回调全部入账，实收 300。
    """

    class _AcceptingGateway:
        name = "fake-gateway"

        def pay(self, order_id, amount, channel):
            return {
                "success": True,
                "pending": True,
                "trade_no": f"TN{order_id}",
                "pay_url": "",
                "qr_code": "",
                "message": "",
            }

        def refund(self, trade_no, amount):
            return {"success": True, "refund_no": f"R{trade_no}", "message": ""}

        def query_transactions(self, db, date):
            return []

    from app.config import settings

    # 回调靠验签认身份，密钥为空时接口直接 503
    monkeypatch.setattr(settings, "payment_gateway_key", "test-money-gateway-key")
    monkeypatch.setitem(billing._GATEWAYS, "gateway", _AcceptingGateway())
    code = _charge_item(client, admin, "RACE-GW", 100)
    settlement, _ = _encounter_settlement(client, admin, org, "扫码重复患者", code, 1)

    first = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "gateway"},
        headers=admin,
    )
    assert first.status_code == 201 and first.json()["status"] == "pending", first.text
    second = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "gateway"},
        headers=admin,
    )
    assert second.status_code == 422, (
        f"第一张 pending 单已占住全部额度，第二次扫码必须被拦：{second.text}"
    )

    # 兜底防线：就算库里已经躺着多张 pending（本次修复之前留下的存量），
    # 回调入账前的未付余额复核也要把第二笔拦下。
    with SessionLocal() as db:
        order = PaymentOrder(
            settlement_id=settlement["id"],
            channel="gateway",
            amount=100,
            status="pending",
            trade_no="TN-LEGACY",
            created_by=1,
        )
        db.add(order)
        db.commit()
        legacy_id = order.id
        first_id = first.json()["id"]

    assert _callback(client, first_id, 10000, "TN1")[0] == 200
    status_code, body = _callback(client, legacy_id, 10000, "TN-LEGACY")
    assert status_code == 409, f"账单已收足，第二笔回调必须拒绝入账：{status_code} {body}"

    orders = client.get(
        "/api/billing/payments", params={"settlement_id": settlement["id"]}, headers=admin
    ).json()
    paid = round(sum(o["amount"] for o in orders if o["status"] == "paid"), 2)
    assert paid == 100.0, f"实收不得超过账单总额，实际 {paid}"


def _callback(client, order_id, amount_fen, trade_no):
    """按 egress 口径签名后调回调接口（网关没有平台账号，身份靠验签）。"""
    import json as _json

    from app.config import settings
    from app.egress import signed_headers

    body = _json.dumps(
        {
            "order_id": order_id,
            "trade_no": trade_no,
            "status": "paid",
            "amount_fen": amount_fen,
        }
    ).encode("utf-8")
    headers = signed_headers(settings.payment_gateway_key, body)
    resp = client.post(
        "/api/billing/payments/callback",
        content=body,
        headers={**headers, "Content-Type": "application/json"},
    )
    return resp.status_code, resp.text


# ---------- B-4 退款 ----------


def test_并发退款的台账与通道笔数一致(client, admin, org):
    """200 元支付单八路并发各退 50：只能成 4 笔，台账 refunded_amount=200。

    改回旧写法（读 refunded_amount → 判可退 → 回写，且通道调用在提交前）时，
    八笔全部 200——通道被真退了 400 元，本地台账只记到 100~150。
    退出去的钱比账上多，这类差错要靠日终对账才发现。
    """
    code = _charge_item(client, admin, "RACE-REFUND", 100)
    settlement, _ = _encounter_settlement(client, admin, org, "并发退款患者", code, 2)
    order = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "cash"},
        headers=admin,
    ).json()
    assert order["amount"] == 200.0 and order["status"] == "paid", order

    def refund(_i):
        resp = client.post(
            f"/api/billing/payments/{order['id']}/refund",
            json={"amount": 50, "reason": "并发退款"},
            headers=admin,
        )
        return resp.status_code

    codes = _race(refund, times=8)
    assert codes.count(200) == 4, f"200 元最多退 4 笔 50，实际成功 {codes.count(200)} 笔：{codes}"
    assert 500 not in codes, f"抢输的那几路应是 422 而不是 500：{codes}"

    current = client.get(
        "/api/billing/payments", params={"settlement_id": settlement["id"]}, headers=admin
    ).json()[0]
    assert current["refunded_amount"] == 200.0, (
        f"通道退了 {50 * codes.count(200)} 元，台账必须记同样多，实际 {current['refunded_amount']}"
    )
    assert current["status"] == "refunded"


def test_全额退款后可以换渠道重新收款(client, admin, org):
    """退掉的钱不该继续占着额度：100 元账单退款后仍能再收 100。

    `paid_already` 原先按 amount 计入 refunded 单，一张账单退过一次就再也
    收不了钱（收银台只能改单据，患者改用另一渠道付会被 422 挡住）。
    改成按净额 `amount - refunded_amount` 计入。
    """
    code = _charge_item(client, admin, "REFUND-RECOLLECT", 100)
    settlement, _ = _encounter_settlement(client, admin, org, "退款再收款患者", code, 1)
    order = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "cash"},
        headers=admin,
    ).json()
    refunded = client.post(
        f"/api/billing/payments/{order['id']}/refund",
        json={"reason": "收错渠道"},
        headers=admin,
    )
    assert refunded.status_code == 200 and refunded.json()["status"] == "refunded"

    again = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "card"},
        headers=admin,
    )
    assert again.status_code == 201, f"全额退款后应能重新收款：{again.text}"
    assert again.json()["status"] == "paid"
