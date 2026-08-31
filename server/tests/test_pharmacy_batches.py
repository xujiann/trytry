"""药品批号效期与西药发药（工程包 B1）。

重点盯三件事：

1. **对账不变式**：DrugStock 汇总量 == Σ(批次量 - 批次已用)。入库、发药、
   退药冲销都在同一事务两边同改，任何一步只改一边，盘点就永远对不上。
2. **FEFO 与过期拒发**：先到效期先出；过期/召回批次一支都不发。
3. **并发不超扣**：批次占用是原子的（claim_quota），8 张方抢 5 支药只能发出 5 张。
"""
import threading

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

TODAY = "2026-08-21"


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "批次县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def patient(client, admin):
    return client.post(
        "/api/patients",
        json={"name": "批次患者", "id_card": "330000199001013333"},
        headers=admin,
    ).json()


def _receive(client, admin, org_id, code, batch_no, expire, qty, name="批次药"):
    return client.post(
        "/api/pharmacy/batches",
        json={"org_id": org_id, "drug_code": code, "drug_name": name,
              "batch_no": batch_no, "expire_date": expire, "quantity": qty},
        headers=admin,
    )


def _rx(client, admin, patient_id, org_id, code, name, daily_dose=1, days=1):
    """开一张（无规则命中、系统审直接通过的）处方，返回处方 JSON。"""
    resp = client.post(
        "/api/prescriptions",
        json={"patient_id": patient_id, "org_id": org_id, "diagnosis_name": "上呼吸道感染",
              "items": [{"drug_code": code, "drug_name": name,
                         "daily_dose": daily_dose, "days": days}]},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    rx = resp.json()
    assert rx["status"] == "auto_passed"
    return rx


def _stock_qty(client, admin, org_id, code):
    rows = client.get("/api/pharmacy/stocks", params={"org_id": org_id}, headers=admin).json()
    mine = [r for r in rows if r["drug_code"] == code]
    return mine[0]["quantity"] if mine else None


def _batches(client, admin, org_id, code):
    return client.get(
        "/api/pharmacy/batches", params={"org_id": org_id, "drug_code": code}, headers=admin
    ).json()


def _assert_invariant(client, admin, org_id, code):
    """对账不变式：汇总量 == Σ(批次量 - 批次已用)。"""
    stock = _stock_qty(client, admin, org_id, code)
    batches = _batches(client, admin, org_id, code)
    batch_sum = sum(b["quantity"] - b["used_quantity"] for b in batches)
    assert stock == batch_sum, f"汇总 {stock} 与批次余量合计 {batch_sum} 对不上账"


# ================================================================ 入库与对账


def test_批次入库落明细并累加汇总(client, admin, org):
    assert _receive(client, admin, org["id"], "AMX", "LOT-A", "2031-01-01", 10).status_code == 201
    assert _receive(client, admin, org["id"], "AMX", "LOT-B", "2030-10-01", 5).status_code == 201
    assert _stock_qty(client, admin, org["id"], "AMX") == 15
    assert len(_batches(client, admin, org["id"], "AMX")) == 2
    _assert_invariant(client, admin, org["id"], "AMX")


def test_同批号再次到货累加_效期不一致拒收(client, admin, org):
    assert _receive(client, admin, org["id"], "AMX", "LOT-A", "2031-01-01", 3).status_code == 201
    batches = {b["batch_no"]: b for b in _batches(client, admin, org["id"], "AMX")}
    assert batches["LOT-A"]["quantity"] == 13
    assert _stock_qty(client, admin, org["id"], "AMX") == 18
    # 同一批号报两个效期，说明录错了：拦下而不是悄悄覆盖
    resp = _receive(client, admin, org["id"], "AMX", "LOT-A", "2032-06-01", 1)
    assert resp.status_code == 422
    _assert_invariant(client, admin, org["id"], "AMX")


# ================================================================ 近效期预警


def test_近效期预警_剩余天数算好并按到期先后排序(client, admin, org):
    _receive(client, admin, org["id"], "EXP", "E-NEAR", "2026-09-10", 4, name="临期药")
    _receive(client, admin, org["id"], "EXP", "E-GONE", "2026-08-01", 2, name="临期药")
    _receive(client, admin, org["id"], "EXP", "E-FAR", "2028-01-01", 6, name="临期药")
    rows = client.get(
        "/api/pharmacy/batches/expiring",
        params={"org_id": org["id"], "today": TODAY},  # 默认 90 天窗口
        headers=admin,
    ).json()
    mine = [r for r in rows if r["drug_code"] == "EXP"]
    assert [r["batch_no"] for r in mine] == ["E-GONE", "E-NEAR"], "应按到期先后排序且远期不入列"
    gone = mine[0]
    assert gone["expired"] is True and gone["remaining_days"] == -20
    near = mine[1]
    assert near["expired"] is False and near["remaining_days"] == 20
    # 窗口参数化：3 天窗口内只剩已过期那批
    rows3 = client.get(
        "/api/pharmacy/batches/expiring",
        params={"org_id": org["id"], "today": TODAY, "days": 3},
        headers=admin,
    ).json()
    assert [r["batch_no"] for r in rows3 if r["drug_code"] == "EXP"] == ["E-GONE"]


# ================================================================ FEFO 与过期拒发


def test_FEFO先到效期先出_跨批次扣减(client, admin, org, patient):
    """LOT-B（2030-10-01）比 LOT-A（2031-01-01）先到期，必须先出 LOT-B。

    发 6 支：LOT-B 余 5 全出，剩 1 支从 LOT-A 出——断言精确到每一批的扣量，
    这是 FEFO 的非空洞证据，不是"发出去了就算对"。
    """
    rx = _rx(client, admin, patient["id"], org["id"], "AMX", "阿莫西林", daily_dose=6, days=1)
    resp = client.post("/api/dispense", json={"prescription_id": rx["id"]}, headers=admin)
    assert resp.status_code == 201, resp.text
    record = resp.json()
    by_batch = {i["batch_no"]: i["quantity"] for i in record["items"]}
    assert by_batch == {"LOT-B": 5, "LOT-A": 1}, f"FEFO 扣减错了：{by_batch}"
    assert record["items"][0]["batch_no"] == "LOT-B", "先到效期的批次应排在明细最前"
    batches = {b["batch_no"]: b for b in _batches(client, admin, org["id"], "AMX")}
    assert batches["LOT-B"]["used_quantity"] == 5
    assert batches["LOT-A"]["used_quantity"] == 1
    assert _stock_qty(client, admin, org["id"], "AMX") == 12
    _assert_invariant(client, admin, org["id"], "AMX")


def test_过期批次拒发_且不留半截记录(client, admin, org, patient):
    """只剩过期批次时整单拒发（409），且批次一支不扣、发药记录一行不留。"""
    _receive(client, admin, org["id"], "OLD", "O-1", "2020-01-01", 10, name="过期药")
    rx = _rx(client, admin, patient["id"], org["id"], "OLD", "过期药")
    resp = client.post("/api/dispense", json={"prescription_id": rx["id"]}, headers=admin)
    assert resp.status_code == 409
    assert "过期" in resp.json()["detail"]
    batch = _batches(client, admin, org["id"], "OLD")[0]
    assert batch["used_quantity"] == 0, "拒发后不得留下半截扣减"
    assert client.get(
        "/api/dispense", params={"prescription_id": rx["id"]}, headers=admin
    ).json() == [], "拒发后不得留下发药记录"
    _assert_invariant(client, admin, org["id"], "OLD")


def test_未审方通过的处方拒发(client, admin, org, patient):
    # 造一条 pending_review：先建规则让日剂量超限
    client.post(
        "/api/prescriptions/rules",
        json={"drug_code": "PND", "max_daily_dose": 1, "dose_unit": "mg"},
        headers=admin,
    )
    resp = client.post(
        "/api/prescriptions",
        json={"patient_id": patient["id"], "org_id": org["id"], "diagnosis_name": "测试",
              "items": [{"drug_code": "PND", "drug_name": "待审药", "daily_dose": 9, "days": 1}]},
        headers=admin,
    )
    rx = resp.json()
    assert rx["status"] == "pending_review"
    out = client.post("/api/dispense", json={"prescription_id": rx["id"]}, headers=admin)
    assert out.status_code == 409
    assert "不可发药" in out.json()["detail"]


# ================================================================ 重复发药与退药冲销


def test_重复发药409(client, admin, org, patient):
    rx = _rx(client, admin, patient["id"], org["id"], "AMX", "阿莫西林")
    assert client.post("/api/dispense", json={"prescription_id": rx["id"]}, headers=admin).status_code == 201
    again = client.post("/api/dispense", json={"prescription_id": rx["id"]}, headers=admin)
    assert again.status_code == 409
    assert "重复" in again.json()["detail"]
    records = client.get("/api/dispense", params={"prescription_id": rx["id"]}, headers=admin).json()
    assert len(records) == 1


def test_退药走冲销不删行_台账两侧回补(client, admin, org, patient):
    stock_before = _stock_qty(client, admin, org["id"], "AMX")
    rx = _rx(client, admin, patient["id"], org["id"], "AMX", "阿莫西林", daily_dose=2, days=1)
    record = client.post("/api/dispense", json={"prescription_id": rx["id"]}, headers=admin).json()
    assert _stock_qty(client, admin, org["id"], "AMX") == stock_before - 2
    rev = client.post(
        f"/api/dispense/{record['id']}/reverse", json={"reason": "患者拒收"}, headers=admin
    )
    assert rev.status_code == 200, rev.text
    body = rev.json()
    assert body["status"] == "reversed" and body["reverse_reason"] == "患者拒收"
    assert body["reversed_at"] is not None
    assert len(body["items"]) == len(record["items"]), "冲销不删明细行"
    assert _stock_qty(client, admin, org["id"], "AMX") == stock_before, "退药后汇总应回补"
    _assert_invariant(client, admin, org["id"], "AMX")
    # 重复冲销拒绝
    assert client.post(
        f"/api/dispense/{record['id']}/reverse", json={"reason": "再退一次"}, headers=admin
    ).status_code == 409


# ================================================================ 召回与批号反查


def test_召回批次拒发_按批号反查发给了谁(client, admin, org, patient):
    _receive(client, admin, org["id"], "RCL", "R-1", "2031-05-01", 5, name="召回药")
    rx = _rx(client, admin, patient["id"], org["id"], "RCL", "召回药", daily_dose=2, days=1)
    client.post("/api/dispense", json={"prescription_id": rx["id"]}, headers=admin)
    batch = _batches(client, admin, org["id"], "RCL")[0]
    # 反查：这一批发给了谁——召回时唯一有用的查询
    trace = client.get(f"/api/pharmacy/batches/{batch['id']}/dispenses", headers=admin).json()
    assert trace["batch_no"] == "R-1" and trace["total_dispensed"] == 2
    assert trace["dispenses"][0]["patient_id"] == patient["id"]
    assert trace["dispenses"][0]["patient_name"] == "批次患者"
    # 召回后拒发、拒再入库
    assert client.post(
        f"/api/pharmacy/batches/{batch['id']}/recall", json={"reason": "厂家召回"}, headers=admin
    ).status_code == 200
    rx2 = _rx(client, admin, patient["id"], org["id"], "RCL", "召回药")
    assert client.post("/api/dispense", json={"prescription_id": rx2["id"]}, headers=admin).status_code == 409
    assert _receive(client, admin, org["id"], "RCL", "R-1", "2031-05-01", 1).status_code == 409


# ================================================================ 并发不超扣


def test_并发发药不超扣批次(client, admin, org, patient):
    """8 张审过的方同时抢 5 支药：只能发出 5 张，台账严丝合缝。

    与 test_stage14 的 _race 同法：Barrier 卡住所有线程一起放行——
    不真并发的并发用例测不出竞态（旧读-改-写写法照样全绿）。
    """
    _receive(client, admin, org["id"], "RACE", "RC-1", "2031-03-01", 5, name="并发药")
    rx_ids = [
        _rx(client, admin, patient["id"], org["id"], "RACE", "并发药")["id"] for _ in range(8)
    ]
    codes: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(rx_ids))

    def run(rx_id):
        barrier.wait(timeout=30)
        code = client.post("/api/dispense", json={"prescription_id": rx_id}, headers=admin).status_code
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=run, args=(i,)) for i in rx_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert codes.count(201) == 5, f"库存 5 支却发出 {codes.count(201)} 张方：{sorted(codes)}"
    assert codes.count(409) == 3
    batch = _batches(client, admin, org["id"], "RACE")[0]
    assert batch["used_quantity"] == 5 and batch["quantity"] == 5
    assert _stock_qty(client, admin, org["id"], "RACE") == 0
    records = client.get("/api/dispense", params={"org_id": org["id"]}, headers=admin).json()
    race_records = [r for r in records if r["prescription_id"] in rx_ids]
    assert len(race_records) == 5, "发药记录数必须等于成功笔数——台账与实际要对得上"
    _assert_invariant(client, admin, org["id"], "RACE")
