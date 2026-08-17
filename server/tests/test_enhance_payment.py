"""E2 医保支付方式改革：DRG/DIP 实际结算 + 医保智能审核。

守四件事：主数据（费率/目录/点值/规则）唯一性与 admin 归口、按病组/病种结算的
盈亏口径（应支付 − 实际成本）、审核规则命中落疑点、以及横向隔离（非全域经办
不得越机构结算/审核）。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def setup(client, admin):
    """两家机构（A 县医院 / B 卫生院）、各自经办、A 家一张病床与一名患者住院。"""
    orgs = {}
    for key, name in [("a", "支付改革A县医院"), ("b", "支付改革B卫生院")]:
        orgs[key] = client.post(
            "/api/organizations",
            json={"name": name, "org_type": "lead_hospital", "level": "county"},
            headers=admin,
        ).json()
    # 各机构一名经办（非全域，用于横向隔离验证）
    for key in ("a", "b"):
        client.post(
            "/api/users",
            json={"username": f"pay_op_{key}", "password": "passw0rd1",
                  "role": "operator", "org_id": orgs[key]["id"]},
            headers=admin,
        )
    op_a = login(client, "pay_op_a", "passw0rd1")
    op_b = login(client, "pay_op_b", "passw0rd1")
    # 一名全域 director（配置只读/统计）
    client.post(
        "/api/users",
        json={"username": "pay_dir", "password": "passw0rd1", "role": "director",
              "org_id": orgs["a"]["id"]},
        headers=admin,
    )
    director = login(client, "pay_dir", "passw0rd1")

    ward = client.post(
        "/api/inpatient/wards", json={"org_id": orgs["a"]["id"], "name": "内科病区"}, headers=admin
    ).json()
    beds = [
        client.post(
            "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": f"A-{i}"}, headers=admin
        ).json()
        for i in range(1, 4)
    ]
    patient = client.post(
        "/api/patients", json={"name": "支付患者", "id_card": "330100199001010011"}, headers=admin
    ).json()
    return {"orgs": orgs, "op_a": op_a, "op_b": op_b, "director": director,
            "ward": ward, "beds": beds, "patient": patient}


def _admission(client, admin, setup, bed_idx, diagnosis, cost, discharge=True):
    """入院→（可选）病案首页+出院自动入组，返回 admission 响应。"""
    adm = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": setup["patient"]["id"], "ward_id": setup["ward"]["id"],
              "bed_id": setup["beds"][bed_idx]["id"], "diagnosis_name": diagnosis},
        headers=admin,
    ).json()
    if discharge:
        client.post(
            f"/api/inpatient/admissions/{adm['id']}/case-summary",
            json={"discharge_diagnosis": diagnosis, "total_cost": cost, "outcome": "好转"},
            headers=admin,
        )
        r = client.post(f"/api/inpatient/admissions/{adm['id']}/discharge", headers=admin)
        assert r.status_code == 200, r.text
    return adm


# ---------------- 主数据 CRUD + 409 ----------------


def test_drg费率唯一性(client, admin):
    ok = client.post("/api/drg-rates",
                     json={"year": "2026", "region": "县域", "rate_per_weight": 1000},
                     headers=admin)
    assert ok.status_code == 201, ok.text
    dup = client.post("/api/drg-rates",
                      json={"year": "2026", "region": "县域", "rate_per_weight": 999},
                      headers=admin)
    assert dup.status_code == 409
    # 同年不同统筹区可并存
    assert client.post("/api/drg-rates",
                       json={"year": "2026", "region": "", "rate_per_weight": 900},
                       headers=admin).status_code == 201
    listed = client.get("/api/drg-rates?year=2026", headers=admin).json()
    assert len(listed) == 2


def test_dip目录与点值(client, admin):
    ok = client.post("/api/dip-catalog",
                     json={"disease_code": "DIP001", "disease_name": "阑尾炎", "score": 100},
                     headers=admin)
    assert ok.status_code == 201, ok.text
    dup = client.post("/api/dip-catalog",
                      json={"disease_code": "DIP001", "disease_name": "重复", "score": 1},
                      headers=admin)
    assert dup.status_code == 409
    patched = client.patch(f"/api/dip-catalog/{ok.json()['id']}",
                           json={"score": 120}, headers=admin)
    assert patched.json()["score"] == 120

    rate = client.post("/api/dip-rates", json={"year": "2026", "point_value": 12.5}, headers=admin)
    assert rate.status_code == 201, rate.text
    dup_rate = client.post("/api/dip-rates", json={"year": "2026", "point_value": 9}, headers=admin)
    assert dup_rate.status_code == 409
    assert any(r["year"] == "2026" for r in client.get("/api/dip-rates", headers=admin).json())


def test_主数据限admin(client, setup):
    denied = client.post("/api/drg-rates",
                         json={"year": "2099", "rate_per_weight": 1}, headers=setup["op_a"])
    assert denied.status_code == 403


# ---------------- 结算：盈亏口径 ----------------


def test_dip结算盈亏口径(client, admin, setup):
    """DIP：应支付 = 分值×点值；实际成本取病案首页费用；盈亏 = 应支付 − 实际。"""
    adm = _admission(client, admin, setup, 0, "急性阑尾炎", 800.0)
    resp = client.post("/api/payment/settle-case",
                       json={"admission_id": adm["id"], "pay_mode": "dip",
                             "year": "2026", "disease_code": "DIP001"},
                       headers=setup["op_a"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # 分值 120（上面 PATCH 后）× 点值 12.5 = 1500
    assert body["weight_or_score"] == 120
    assert body["standard_payment"] == 1500.0
    assert body["actual_cost"] == 800.0
    assert body["profit"] == round(body["standard_payment"] - body["actual_cost"], 2)
    assert body["profit"] == 700.0


def test_drg结算读病案首页权重(client, admin, setup):
    """DRG：读该住院入组权重×费率；实际成本取病案首页费用。"""
    adm = _admission(client, admin, setup, 1, "社区获得性肺炎", 6000.0)
    resp = client.post("/api/payment/settle-case",
                       json={"admission_id": adm["id"], "pay_mode": "drg",
                             "year": "2026", "region": "县域"},
                       headers=setup["op_a"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["weight_or_score"] > 0  # 已入组
    assert body["standard_payment"] == round(body["weight_or_score"] * 1000, 2)
    assert body["actual_cost"] == 6000.0
    assert body["profit"] == round(body["standard_payment"] - body["actual_cost"], 2)


def test_缺费率409(client, admin, setup):
    adm = _admission(client, admin, setup, 2, "急性阑尾炎", 500.0)
    # 该年度无 DIP 点值
    miss = client.post("/api/payment/settle-case",
                       json={"admission_id": adm["id"], "pay_mode": "dip",
                             "year": "2099", "disease_code": "DIP001"},
                       headers=setup["op_a"])
    assert miss.status_code == 409


def test_跨机构结算被拒(client, admin, setup):
    """横向隔离：B 家经办不得为 A 家的住院做结算测算。"""
    adm = _admission(client, admin, setup, 0, "急性阑尾炎", 800.0, discharge=False)
    denied = client.post("/api/payment/settle-case",
                         json={"admission_id": adm["id"], "pay_mode": "dip",
                               "year": "2026", "disease_code": "DIP001"},
                         headers=setup["op_b"])
    assert denied.status_code == 403


def test_病例清单与盈亏统计(client, setup):
    cases = client.get("/api/payment/cases?org_id=" + str(setup["orgs"]["a"]["id"]),
                       headers=setup["op_a"]).json()
    assert len(cases) >= 2
    stats = client.get("/api/payment/profit-stats?year=2026", headers=setup["director"]).json()
    modes = {m["pay_mode"] for m in stats["by_pay_mode"]}
    assert {"dip", "drg"} <= modes
    assert stats["total"]["count"] >= 2


# ---------------- 智能审核 ----------------


@pytest.fixture(scope="module")
def ins_settlement(client, setup):
    """A 家一条医保结算记录（总额 5000），供审核命中。"""
    resp = client.post(
        "/api/insurance/settlements",
        json={"patient_id": setup["patient"]["id"], "org_id": setup["orgs"]["a"]["id"],
              "settle_type": "local", "total_amount": 5000, "insurance_pay": 3000, "self_pay": 2000},
        headers=setup["op_a"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_审核规则创建(client, admin):
    ok = client.post("/api/insurance/audit-rules",
                     json={"code": "OVERAMT", "name": "单次超额", "rule_type": "over_amount",
                           "params": {"max": 1000}},
                     headers=admin)
    assert ok.status_code == 201, ok.text
    dup = client.post("/api/insurance/audit-rules",
                      json={"code": "OVERAMT", "name": "重复", "rule_type": "over_amount"},
                      headers=admin)
    assert dup.status_code == 409
    assert any(r["code"] == "OVERAMT" for r in client.get("/api/insurance/audit-rules", headers=admin).json())


def test_审核命中落疑点并处置(client, admin, setup, ins_settlement):
    # 确保规则存在（用例独立性）
    client.post("/api/insurance/audit-rules",
                json={"code": "OVERAMT", "name": "单次超额", "rule_type": "over_amount",
                      "params": {"max": 1000}}, headers=admin)
    run = client.post("/api/insurance/audit/run",
                      json={"settlement_id": ins_settlement["id"]}, headers=setup["op_a"])
    assert run.status_code == 201, run.text
    flags = run.json()["flags"]
    assert flags and any(f["rule_code"] == "OVERAMT" for f in flags)
    flag_id = flags[0]["id"]

    listed = client.get("/api/insurance/audit/flags?status=open",
                        headers=setup["op_a"]).json()
    assert any(f["id"] == flag_id for f in listed)

    patched = client.patch(f"/api/insurance/audit/flags/{flag_id}",
                           json={"status": "confirmed"}, headers=setup["op_a"])
    assert patched.status_code == 200 and patched.json()["status"] == "confirmed"


def test_跨机构审核被拒(client, setup, ins_settlement):
    denied = client.post("/api/insurance/audit/run",
                         json={"settlement_id": ins_settlement["id"]}, headers=setup["op_b"])
    assert denied.status_code == 403


def test_budget_apply_guards(client, admin):
    """HIGH-2 回归：预算下发拒绝已清算池(409)、口径不一致池(422)，一致池(200)。"""
    from app.database import SessionLocal
    from app.models import FundPool
    with SessionLocal() as db:
        active = FundPool(year=2026, insurance_type="resident", org_group_id=None, total_amount=0, status="active")
        wrong = FundPool(year=2024, insurance_type="employee", org_group_id=None, total_amount=0, status="active")
        settled = FundPool(year=2028, insurance_type="resident", org_group_id=None, total_amount=100, status="settled")
        db.add_all([active, wrong, settled]); db.commit()
        aid, wid, sid = active.id, wrong.id, settled.id
    plan26 = client.post("/api/payment/budget-plans", json={"year": "2026", "insurance_type": "resident", "base_amount": 1000000, "growth_pct": 8, "per_capita": 50, "headcount": 2000}, headers=admin).json()
    assert client.post(f"/api/payment/budget-plans/{plan26['id']}/apply-to-pool", json={"pool_id": aid}, headers=admin).status_code == 200
    assert client.post(f"/api/payment/budget-plans/{plan26['id']}/apply-to-pool", json={"pool_id": wid}, headers=admin).status_code == 422
    plan28 = client.post("/api/payment/budget-plans", json={"year": "2028", "insurance_type": "resident", "base_amount": 100000}, headers=admin).json()
    assert client.post(f"/api/payment/budget-plans/{plan28['id']}/apply-to-pool", json={"pool_id": sid}, headers=admin).status_code == 409


def test_decompose_audit_fires(client, admin, setup):
    """MED-4 回归：同患者同机构短窗口两笔结算，对称窗口下 decompose_admission 命中。"""
    from app.database import SessionLocal
    from app.models import InsuranceSettlement
    org = setup["orgs"]["a"]["id"]
    pat = client.post("/api/patients", json={"name": "分解住院患者", "id_card": "330100199202020022"}, headers=admin).json()
    with SessionLocal() as db:
        for _ in range(2):
            db.add(InsuranceSettlement(patient_id=pat["id"], org_id=org, settle_type="local", total_amount=1000, insurance_pay=800, self_pay=200))
        db.commit()
        sid = db.query(InsuranceSettlement).filter(
            InsuranceSettlement.patient_id == pat["id"], InsuranceSettlement.org_id == org
        ).order_by(InsuranceSettlement.id.desc()).first().id
    client.post("/api/insurance/audit-rules", json={"code": "DECOMP-MED4", "name": "分解住院", "rule_type": "decompose_admission", "params": {"days": 15, "max_count": 1}}, headers=admin)
    r = client.post("/api/insurance/audit/run", json={"settlement_id": sid}, headers=admin)
    assert r.status_code == 201, r.text
    assert "DECOMP-MED4" in [f["rule_code"] for f in r.json()["flags"]]
