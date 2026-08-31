"""事务边界建档（P1-19）：资金/库存/状态机链路中途失败不得留下半写状态。

全仓此前只有 3 个文件提到 rollback——"链路中途断掉之后库里是什么样子"几乎没有
测试回答过。本文件按六条核心链路补上这张网，每条验证两件事：

1. **中途失败后无半写**：链路里已经执行过的写入（建单/认领/扣减/翻状态）必须
   随失败整体消失，失败前后的库表状态**逐字段一致**——断言对着数据库实查
   （独立会话重读），不对着 HTTP 响应猜；
2. **失败后系统还能用**：同样的请求换个时机重来必须成功且只生效一次——
   rollback 不干净的典型症状（SQLite 写锁被挂死、唯一约束被幽灵行占住、
   状态被翻了一半）都会让这一步露馅。

故障来源分两类，各占其半：

- **真实业务失败**（不打补丁）：链路走到后半段才发现台账不符 / 参数超限 /
  目标批次效期冲突——这些是产品自己的 rollback 分支，网要罩住它们本身；
- **注入 commit 故障**（monkeypatch `app.database.SessionLocal`，套壳会话的
  commit 抛错）：模拟"提交瞬间断连/断电"。写法照抄
  `test_audit_middleware_hardening.py` 的 _CommitBoomSession，只是那边打的是
  app.main 的审计会话，这边打 app.database 的业务会话（两个绑定名互不影响，
  审计留痕照常工作）。

非空洞性（变异验证，见建档报告）：把被测路由的单一事务拆开（`db.flush()` 改
`db.commit()`、或在链路中段插一个 commit），对应用例即红。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

import app.database as database_mod
from app.database import SessionLocal
from app.main import app
from app.models import (
    BillDetail,
    Deposit,
    DispenseItem,
    DispenseRecord,
    DrugBatch,
    DrugStock,
    InsuranceSettlement,
    PaymentOrder,
    Settlement,
    StockTransfer,
)
from app.routers import billing


@pytest.fixture(scope="module")
def client():
    reset_database()
    # raise_server_exceptions=False：注入 commit 故障后要断言的正是"请求以 500
    # 收场、而库里没有半写"——异常抛进用例就看不到这两件事了。
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
        json={"name": "事务边界县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def ward(client, admin, org):
    return client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "事务边界内科"}, headers=admin
    ).json()


# ---------------------------------------------------------------- 注入器


def _arm_commit_boom(monkeypatch):
    """让接下来的请求在**业务会话** commit 时抛错，返回计数器（证明真的炸过）。

    get_db 在调用时才解析 `app.database.SessionLocal`，因此打这个名字对每个
    新请求生效；测试模块顶部 `from app.database import SessionLocal` 拿到的是
    补丁前的真工厂，快照重读不受影响。commit 抛错后不动真会话——挂着未提交
    事务离场，正是断连时的真实形状；写入是否被丢弃由 get_db 的 close 兜底，
    这正是本文件要验证的边界。
    """
    real_factory = database_mod.SessionLocal
    stats = {"attempts": 0}

    class _CommitBoomSession:
        def __init__(self):
            self._real = real_factory()

        def __getattr__(self, item):
            return getattr(self._real, item)

        def commit(self):
            stats["attempts"] += 1
            raise RuntimeError("注入：业务事务 commit 时数据库连接断开")

    monkeypatch.setattr(database_mod, "SessionLocal", _CommitBoomSession)
    return stats


# ---------------------------------------------------------------- 建数据小工具

_seq = {"n": 0}


def _next_id_card() -> str:
    _seq["n"] += 1
    return f"37000019900101{_seq['n']:04d}"


def _patient(client, admin, name):
    return client.post(
        "/api/patients", json={"name": name, "id_card": _next_id_card()}, headers=admin
    ).json()


def _admission(client, admin, ward, name):
    patient = _patient(client, admin, name)
    _seq["n"] += 1
    bed = client.post(
        "/api/inpatient/beds",
        json={"ward_id": ward["id"], "bed_no": f"T{_seq['n']:03d}"},
        headers=admin,
    ).json()
    admission = client.post(
        "/api/inpatient/admissions",
        json={
            "patient_id": patient["id"],
            "ward_id": ward["id"],
            "bed_id": bed["id"],
            "doctor_name": "事务医生",
            "diagnosis_name": "肺炎",
        },
        headers=admin,
    ).json()
    return patient, admission


def _charge_item(client, admin, code, price):
    resp = client.post(
        "/api/billing/charge-items",
        json={"code": code, "name": f"项目{code}", "category": "other", "price": price},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return code


def _receive_batch(client, admin, org_id, code, batch_no, expire, qty, name="事务药"):
    resp = client.post(
        "/api/pharmacy/batches",
        json={"org_id": org_id, "drug_code": code, "drug_name": name,
              "batch_no": batch_no, "expire_date": expire, "quantity": qty},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _rx(client, admin, patient_id, org_id, code, daily_dose, days=1, name="事务药"):
    resp = client.post(
        "/api/prescriptions",
        json={"patient_id": patient_id, "org_id": org_id, "diagnosis_name": "上呼吸道感染",
              "items": [{"drug_code": code, "drug_name": name,
                         "daily_dose": daily_dose, "days": days}]},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] in ("auto_passed", "approved"), body  # 可发药前提
    return body


# ---------------------------------------------------------------- 快照（对库实查）


def _billing_state(admission_id, patient_id):
    """结算链路全景：结算单 / 医保结算 / 明细认领 / 押金流水，逐字段。"""
    with SessionLocal() as db:
        settlements = [
            (s.bill_type, round(float(s.total_amount), 2), round(float(s.insurance_pay), 2),
             round(float(s.self_pay), 2), s.insurance_settlement_id)
            for s in db.query(Settlement).filter(Settlement.admission_id == admission_id)
            .order_by(Settlement.id).all()
        ]
        insurance = [
            (i.settle_type, round(float(i.total_amount), 2), round(float(i.insurance_pay), 2))
            for i in db.query(InsuranceSettlement)
            .filter(InsuranceSettlement.patient_id == patient_id)
            .order_by(InsuranceSettlement.id).all()
        ]
        details = [
            (d.item_code, d.quantity, round(float(d.amount), 2), d.settlement_id)
            for d in db.query(BillDetail).filter(BillDetail.admission_id == admission_id)
            .order_by(BillDetail.id).all()
        ]
        deposits = _deposit_rows(db, admission_id)
    return {"settlements": settlements, "insurance": insurance,
            "details": details, "deposits": deposits}


def _deposit_rows(db, admission_id):
    return [
        (d.deposit_type, round(float(d.amount), 2), d.method, d.operator)
        for d in db.query(Deposit).filter(Deposit.admission_id == admission_id)
        .order_by(Deposit.id).all()
    ]


def _deposit_ledger(admission_id):
    with SessionLocal() as db:
        return _deposit_rows(db, admission_id)


def _pharmacy_state(org_id, code):
    """库存链路全景：汇总 + 每个批次的四个台账字段。"""
    with SessionLocal() as db:
        stock = (
            db.query(DrugStock)
            .filter(DrugStock.org_id == org_id, DrugStock.drug_code == code)
            .first()
        )
        batches = [
            (b.batch_no, b.quantity, b.used_quantity, b.blocked_quantity, b.status)
            for b in db.query(DrugBatch)
            .filter(DrugBatch.org_id == org_id, DrugBatch.drug_code == code)
            .order_by(DrugBatch.id).all()
        ]
        return {"stock": stock.quantity if stock else None, "batches": batches}


def _dispense_rows(prescription_id):
    with SessionLocal() as db:
        records = [
            (r.status, r.reverse_reason, r.reversed_at)
            for r in db.query(DispenseRecord)
            .filter(DispenseRecord.prescription_id == prescription_id)
            .order_by(DispenseRecord.id).all()
        ]
        items = (
            db.query(DispenseItem)
            .join(DispenseRecord, DispenseItem.dispense_id == DispenseRecord.id)
            .filter(DispenseRecord.prescription_id == prescription_id)
            .count()
        )
    return {"records": records, "item_count": items}


def _payment_rows(settlement_id):
    with SessionLocal() as db:
        return [
            (o.channel, round(float(o.amount), 2), o.status,
             round(float(o.refunded_amount), 2))
            for o in db.query(PaymentOrder)
            .filter(PaymentOrder.settlement_id == settlement_id)
            .order_by(PaymentOrder.id).all()
        ]


def _set_batch(batch_id, **values):
    """直接改批次台账列，制造"账实不符"这类真实故障前提。"""
    with SessionLocal() as db:
        db.query(DrugBatch).filter(DrugBatch.id == batch_id).update(values)
        db.commit()


def _set_stock(org_id, code, qty):
    with SessionLocal() as db:
        db.query(DrugStock).filter(
            DrugStock.org_id == org_id, DrugStock.drug_code == code
        ).update({"quantity": qty})
        db.commit()


# ================================================================ 链路一：住院结算
#
# create_settlement 是全仓最长的单事务：建结算单 → UPDATE 认领明细 → 医保结算
# 记录 → 押金冲抵，一个 commit 收尾。任何一段单独提交都是事故（明细被失败的
# 结算占住 / 医保基金重复计入 / 押金白扣）。


def test_结算走到医保校验才失败_明细认领与结算单一并回滚(client, admin, ward):
    """真实失败路径：insurance_pay 超总额的 422 发生在建单、认领明细**之后**。"""
    patient, admission = _admission(client, admin, ward, "结算回滚患者")
    code = _charge_item(client, admin, "TXB-SET1", 100)
    assert client.post(
        "/api/billing/deposits", json={"admission_id": admission["id"], "amount": 500},
        headers=admin,
    ).status_code == 201
    for _ in range(3):
        assert client.post(
            "/api/billing/details",
            json={"patient_id": patient["id"], "admission_id": admission["id"],
                  "item_code": code, "quantity": 1},
            headers=admin,
        ).status_code == 201

    before = _billing_state(admission["id"], patient["id"])
    assert [d[3] for d in before["details"]] == [None, None, None]  # 三条明细都未结

    resp = client.post(
        "/api/billing/settlements",
        json={"bill_type": "inpatient", "admission_id": admission["id"],
              "insurance_pay": 999},  # 超过总额 300，走建单认领之后的 422 分支
        headers=admin,
    )
    assert resp.status_code == 422, resp.text
    assert _billing_state(admission["id"], patient["id"]) == before, (
        "失败的结算不得留下任何痕迹：结算单/医保结算/明细认领/押金都必须原样"
    )

    # 失败后同一张单还能正常结算，且各写入恰好一次
    resp = client.post(
        "/api/billing/settlements",
        json={"bill_type": "inpatient", "admission_id": admission["id"], "insurance_pay": 0},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    after = _billing_state(admission["id"], patient["id"])
    assert after["settlements"] == [("inpatient", 300.0, 0.0, 300.0, None)]
    assert after["insurance"] == []  # 无医保分担就不得落医保结算记录
    assert {d[3] for d in after["details"]} == {resp.json()["id"]}  # 明细全部归属这张单
    assert after["deposits"] == [("prepay", 500.0, "cash", "平台管理员"),
                                 ("offset", 300.0, "settle", "平台管理员")]


def test_结算commit断连_整链无半写且重试恰好生效一次(client, admin, ward, monkeypatch):
    """注入失败路径：链路全部走完、提交那一刻断连。"""
    patient, admission = _admission(client, admin, ward, "结算断连患者")
    code = _charge_item(client, admin, "TXB-SET2", 100)
    client.post(
        "/api/billing/deposits", json={"admission_id": admission["id"], "amount": 500},
        headers=admin,
    )
    for _ in range(3):
        client.post(
            "/api/billing/details",
            json={"patient_id": patient["id"], "admission_id": admission["id"],
                  "item_code": code, "quantity": 1},
            headers=admin,
        )
    before = _billing_state(admission["id"], patient["id"])

    stats = _arm_commit_boom(monkeypatch)
    resp = client.post(
        "/api/billing/settlements",
        json={"bill_type": "inpatient", "admission_id": admission["id"], "insurance_pay": 100},
        headers=admin,
    )
    monkeypatch.undo()
    assert resp.status_code == 500, resp.text
    assert stats["attempts"] >= 1, "注入没生效：这条用例没测到 commit 故障"
    assert _billing_state(admission["id"], patient["id"]) == before, (
        "commit 断连后结算单/医保结算/明细认领/押金冲抵必须整体消失"
    )

    # rollback 干净：同一请求重来成功，医保结算与押金冲抵都只发生一次
    resp = client.post(
        "/api/billing/settlements",
        json={"bill_type": "inpatient", "admission_id": admission["id"], "insurance_pay": 100},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    after = _billing_state(admission["id"], patient["id"])
    assert len(after["settlements"]) == 1
    assert after["settlements"][0][1:4] == (300.0, 100.0, 200.0)
    assert len(after["insurance"]) == 1, "医保结算记录必须恰好一条（基金口径）"
    assert [d for d in after["deposits"] if d[0] == "offset"] == [
        ("offset", 200.0, "settle", "平台管理员")
    ], "押金冲抵必须恰好一次、金额=个人自付"


# ================================================================ 链路二：发药扣库存
#
# dispense 同一事务写发药记录 + 明细 + 批次占用 + 汇总扣减，
# 对账不变式 汇总==Σ(批次量-已用-退回不可发) 只有整体提交/整体回滚才成立。


def test_发药到汇总扣减才失败_批次占用一并回滚(client, admin, org):
    """真实失败路径：批次占得到、汇总不够扣（账实不符 409）发生在链路末段。"""
    patient = _patient(client, admin, "发药回滚患者")
    batch = _receive_batch(client, admin, org["id"], "TXB-DISP1", "DB-1", "2030-06-01", 10)
    _set_stock(org["id"], "TXB-DISP1", 3)  # 制造账实不符：批次可发 10、汇总只剩 3
    rx = _rx(client, admin, patient["id"], org["id"], "TXB-DISP1", daily_dose=10)

    before = _pharmacy_state(org["id"], "TXB-DISP1")
    assert before == {"stock": 3, "batches": [("DB-1", 10, 0, 0, "normal")]}

    resp = client.post("/api/dispense", json={"prescription_id": rx["id"]}, headers=admin)
    assert resp.status_code == 409, resp.text
    assert _pharmacy_state(org["id"], "TXB-DISP1") == before, (
        "汇总扣不动时批次占用必须回滚——否则批次已用凭空多出 10"
    )
    assert _dispense_rows(rx["id"]) == {"records": [], "item_count": 0}

    # 盘点修正汇总后，同一张方要能发出去（唯一约束没有被失败的尝试占住）
    _set_stock(org["id"], "TXB-DISP1", 10)
    resp = client.post("/api/dispense", json={"prescription_id": rx["id"]}, headers=admin)
    assert resp.status_code == 201, resp.text
    assert _pharmacy_state(org["id"], "TXB-DISP1") == {
        "stock": 0, "batches": [("DB-1", 10, 10, 0, "normal")]
    }
    del batch  # 篡改走 _set_stock，批次原值仅用于对照


def test_发药commit断连_台账两侧与发药记录全部回滚(client, admin, org, monkeypatch):
    patient = _patient(client, admin, "发药断连患者")
    _receive_batch(client, admin, org["id"], "TXB-DISP2", "DB-2", "2030-06-01", 10)
    rx = _rx(client, admin, patient["id"], org["id"], "TXB-DISP2", daily_dose=4)
    before = _pharmacy_state(org["id"], "TXB-DISP2")

    stats = _arm_commit_boom(monkeypatch)
    resp = client.post("/api/dispense", json={"prescription_id": rx["id"]}, headers=admin)
    monkeypatch.undo()
    assert resp.status_code == 500, resp.text
    assert stats["attempts"] >= 1
    assert _pharmacy_state(org["id"], "TXB-DISP2") == before
    assert _dispense_rows(rx["id"]) == {"records": [], "item_count": 0}

    # 重试成功且只扣一次（prescription_id 唯一约束未被幽灵行占住）
    resp = client.post("/api/dispense", json={"prescription_id": rx["id"]}, headers=admin)
    assert resp.status_code == 201, resp.text
    assert _pharmacy_state(org["id"], "TXB-DISP2") == {
        "stock": 6, "batches": [("DB-2", 10, 4, 0, "normal")]
    }
    rows = _dispense_rows(rx["id"])
    assert len(rows["records"]) == 1 and rows["records"][0][0] == "dispensed"


# ================================================================ 链路三：退药冲销（状态机）
#
# reverse 的状态闸门（dispensed→reversed 条件 UPDATE）与两侧回补在同一事务：
# 翻了状态却没回补，或回补了一半，都会让台账永远对不上。


def test_冲销途中台账不符_状态翻转必须一并回滚(client, admin, org):
    patient = _patient(client, admin, "冲销回滚患者")
    batch = _receive_batch(client, admin, org["id"], "TXB-REV1", "RB-1", "2030-06-01", 10)
    rx = _rx(client, admin, patient["id"], org["id"], "TXB-REV1", daily_dose=5)
    dispense = client.post("/api/dispense", json={"prescription_id": rx["id"]}, headers=admin)
    assert dispense.status_code == 201, dispense.text
    dispense_id = dispense.json()["id"]

    # 制造台账不符：批次已用被外部改小到 2 < 明细量 5，回补必然失败
    _set_batch(batch["id"], used_quantity=2)
    before = _pharmacy_state(org["id"], "TXB-REV1")

    resp = client.post(
        f"/api/dispense/{dispense_id}/reverse", json={"reason": "患者退药"}, headers=admin
    )
    assert resp.status_code == 409, resp.text
    assert _pharmacy_state(org["id"], "TXB-REV1") == before
    rows = _dispense_rows(rx["id"])
    assert rows["records"] == [("dispensed", "", None)], (
        "回补失败时状态闸门的翻转必须回滚——翻成 reversed 却没回补，"
        "这张单从此既退不了也对不上账"
    )

    # 盘点修正后冲销成功，且状态闸门此后仍然挡重复冲销
    _set_batch(batch["id"], used_quantity=5)
    resp = client.post(
        f"/api/dispense/{dispense_id}/reverse", json={"reason": "患者退药"}, headers=admin
    )
    assert resp.status_code == 200, resp.text
    assert _pharmacy_state(org["id"], "TXB-REV1") == {
        "stock": 10, "batches": [("RB-1", 10, 0, 0, "normal")]
    }
    rows = _dispense_rows(rx["id"])
    assert rows["records"][0][0] == "reversed" and rows["records"][0][1] == "患者退药"
    assert client.post(
        f"/api/dispense/{dispense_id}/reverse", json={"reason": "再退一次"}, headers=admin
    ).status_code == 409


# ================================================================ 链路四：押金退费


def test_退押金commit断连_流水无半写且随后可正常退(client, admin, ward, monkeypatch):
    _, admission = _admission(client, admin, ward, "押金断连患者")
    assert client.post(
        "/api/billing/deposits", json={"admission_id": admission["id"], "amount": 300},
        headers=admin,
    ).status_code == 201
    before = _deposit_ledger(admission["id"])
    assert before == [("prepay", 300.0, "cash", "平台管理员")]

    stats = _arm_commit_boom(monkeypatch)
    resp = client.post(
        "/api/billing/deposits/refund",
        json={"admission_id": admission["id"], "amount": 100},
        headers=admin,
    )
    monkeypatch.undo()
    assert resp.status_code == 500, resp.text
    assert stats["attempts"] >= 1
    assert _deposit_ledger(admission["id"]) == before, "断连的退费不得留下流水行"

    # rollback 干净：金额闸门（临界区 + INSERT..SELECT）随后照常工作
    resp = client.post(
        "/api/billing/deposits/refund",
        json={"admission_id": admission["id"], "amount": 100},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    assert _deposit_ledger(admission["id"]) == before + [("refund", 100.0, "cash", "平台管理员")]
    balance = client.get(
        "/api/billing/deposits/balance", params={"admission_id": admission["id"]}, headers=admin
    ).json()
    assert (balance["refunded"], balance["balance"]) == (100.0, 200.0)
    # 超余额退费仍被拦、且不落行——失败请求没有破坏后续判定
    assert client.post(
        "/api/billing/deposits/refund",
        json={"admission_id": admission["id"], "amount": 250},
        headers=admin,
    ).status_code == 422
    assert len(_deposit_ledger(admission["id"])) == 2


# ================================================================ 链路五：缴费下单
#
# create_payment 在同一事务里落支付单并调通道。通道**返回失败**是产品处理过的
# 分支（置 failed）；通道**抛异常**（超时/崩溃）则必须整体回滚——半写的支付单
# 会永久占住结算单额度（_collected_amount 把 pending 也计入）。


def test_通道抛异常_支付单不得半写且额度不被占用(client, admin, org, monkeypatch):
    patient = _patient(client, admin, "通道异常患者")
    encounter = client.post(
        "/api/encounters",
        json={"patient_id": patient["id"], "org_id": org["id"], "encounter_type": "outpatient"},
        headers=admin,
    ).json()
    code = _charge_item(client, admin, "TXB-PAY1", 100)
    client.post(
        "/api/billing/details",
        json={"patient_id": patient["id"], "encounter_id": encounter["id"],
              "item_code": code, "quantity": 1},
        headers=admin,
    )
    settlement = client.post(
        "/api/billing/settlements",
        json={"bill_type": "outpatient", "encounter_id": encounter["id"]},
        headers=admin,
    ).json()

    def crash(order_id, amount, channel):
        raise RuntimeError("通道超时崩溃（注入）")

    monkeypatch.setattr(billing.MOCK_GATEWAY, "pay", crash)
    resp = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "cash", "amount": 100},
        headers=admin,
    )
    monkeypatch.undo()
    assert resp.status_code == 500, resp.text
    assert _payment_rows(settlement["id"]) == [], (
        "通道抛异常时支付单必须整体回滚——半写的 pending 单会把结算单额度占死"
    )

    # 额度没有被幽灵单占住：换个时机全额收款成功，且只有这一笔
    resp = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "cash", "amount": 100},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    assert _payment_rows(settlement["id"]) == [("cash", 100.0, "paid", 0.0)]


# ================================================================ 链路六：跨机构调拨
#
# transfer_stock 在一个事务里改四处：调出批次占用、调出汇总、调入汇总、调入
# 批次（外加调拨流水）。半写意味着药"在途蒸发"或两边同时记账。


def test_调拨到目标批次效期冲突才失败_两侧库存原封不动(client, admin, org):
    township = client.post(
        "/api/organizations",
        json={"name": "事务边界卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    village = client.post(
        "/api/organizations",
        json={"name": "事务边界村卫生室", "org_type": "village", "level": "village"},
        headers=admin,
    ).json()
    _receive_batch(client, admin, org["id"], "TXB-MOVE", "MB-1", "2030-06-01", 60)
    # 调入方已按另一个效期登记同批号：冲突发生在调出占用、两侧汇总都改完之后
    _receive_batch(client, admin, township["id"], "TXB-MOVE", "MB-1", "2031-01-01", 5)

    before_src = _pharmacy_state(org["id"], "TXB-MOVE")
    before_dst = _pharmacy_state(township["id"], "TXB-MOVE")

    resp = client.post(
        "/api/pharmacy/transfers",
        json={"drug_code": "TXB-MOVE", "from_org_id": org["id"],
              "to_org_id": township["id"], "quantity": 40},
        headers=admin,
    )
    assert resp.status_code == 409, resp.text
    assert _pharmacy_state(org["id"], "TXB-MOVE") == before_src, (
        "效期冲突拦下的调拨不得在调出侧留下扣减——那 40 片会在途蒸发"
    )
    assert _pharmacy_state(township["id"], "TXB-MOVE") == before_dst, (
        "调入侧汇总也必须原样——只加汇总不落批次就是幽灵库存"
    )
    with SessionLocal() as db:
        assert db.query(StockTransfer).filter(StockTransfer.drug_code == "TXB-MOVE").count() == 0

    # 失败后调往无冲突的机构照常成立，且两侧各记恰好一次
    resp = client.post(
        "/api/pharmacy/transfers",
        json={"drug_code": "TXB-MOVE", "from_org_id": org["id"],
              "to_org_id": village["id"], "quantity": 40},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    assert _pharmacy_state(org["id"], "TXB-MOVE") == {
        "stock": 20, "batches": [("MB-1", 60, 40, 0, "normal")]
    }
    assert _pharmacy_state(village["id"], "TXB-MOVE") == {
        "stock": 40, "batches": [("MB-1", 40, 0, 0, "normal")]
    }
    with SessionLocal() as db:
        assert db.query(StockTransfer).filter(StockTransfer.drug_code == "TXB-MOVE").count() == 1
