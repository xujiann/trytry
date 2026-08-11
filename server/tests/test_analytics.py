"""阶段四：县外就诊与就医流向、运行效率、自定义绩效公式与受限求值器。"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.formula import FormulaError, evaluate, validate
from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def setup(client, admin):
    county = client.post(
        "/api/organizations", json={"name": "指标演示县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    town = client.post(
        "/api/organizations", json={"name": "指标演示卫生院", "org_type": "township", "level": "township",
                                    "parent_id": county["id"]},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients", json={"name": "指标患者", "id_card": "331382199001011234"}, headers=admin
    ).json()
    return {"county": county, "town": town, "patient": patient}


# ============================================================================
# 受限表达式求值器（安全性是重点）
# ============================================================================


def test_formula_basic_arithmetic():
    assert evaluate("a + b * 2", {"a": 1, "b": 3}) == 7
    assert evaluate("(a + b) / 2", {"a": 1, "b": 3}) == 2
    assert evaluate("-a + abs(-5)", {"a": 2}) == 3
    assert evaluate("round(a / b * 100, 1)", {"a": 1, "b": 3}) == 33.3
    assert evaluate("min(a, b) + max(a, b)", {"a": 4, "b": 9}) == 13


def test_formula_division_by_zero_returns_zero():
    """分母为零在绩效公式里是常态（本期无业务量），不能让整张报表出不来。"""
    assert evaluate("a / b", {"a": 10, "b": 0}) == 0
    assert evaluate("a % b", {"a": 10, "b": 0}) == 0


def test_formula_rejects_unknown_variable():
    with pytest.raises(FormulaError, match="未知变量"):
        evaluate("a + nope", {"a": 1})


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('ls')",
        "().__class__",
        "a.__class__",
        "[x for x in range(10)]",
        "lambda: 1",
        "open('/etc/passwd')",
        "a if a else b",
        "a[0]",
        "{'k': 1}",
    ],
)
def test_formula_rejects_dangerous_syntax(expression):
    """公式由管理员录入，等同于让用户往服务端塞代码——必须逐类拒绝。"""
    with pytest.raises(FormulaError):
        evaluate(expression, {"a": 1, "b": 2})


def test_formula_rejects_huge_power():
    """2**999999 能把进程算到内存耗尽。"""
    with pytest.raises(FormulaError, match="幂指数"):
        evaluate("a ** 999999", {"a": 2})
    assert evaluate("a ** 2", {"a": 3}) == 9


def test_formula_rejects_too_long_expression():
    with pytest.raises(FormulaError, match="表达式长度"):
        evaluate("a+" * 400 + "a", {"a": 1})


def test_formula_validate_uses_dummy_values():
    """录入校验不该因为哑值除零而误报。"""
    validate("a / b", {"a", "b"})
    with pytest.raises(FormulaError):
        validate("a / c", {"a", "b"})


# ============================================================================
# T4.1 县外就诊与就医流向
# ============================================================================


def test_patient_flow_rates(client, admin, setup):
    # 县内 3 次就诊
    for i in range(3):
        client.post(
            "/api/encounters",
            json={"patient_id": setup["patient"]["id"], "org_id": setup["town"]["id"],
                  "doctor_name": "全科医生", "diagnosis_name": f"上感{i}"},
            headers=admin,
        )
    # 县外 1 次（经转诊）+ 1 次（自行外出）
    referral = client.post(
        "/api/referrals",
        json={"patient_id": setup["patient"]["id"], "from_org_id": setup["town"]["id"],
              "to_org_id": setup["county"]["id"], "direction": "up", "reason": "需上级诊治"},
        headers=admin,
    ).json()
    assert client.post(
        "/api/analytics/outbound-visits",
        json={"patient_id": setup["patient"]["id"], "visit_date": "2026-08-05",
              "external_org_name": "市第一人民医院", "external_org_level": "city",
              "visit_type": "inpatient", "total_amount": 22000, "insurance_pay": 15000,
              "referral_id": referral["id"]},
        headers=admin,
    ).status_code == 201
    client.post(
        "/api/analytics/outbound-visits",
        json={"patient_id": setup["patient"]["id"], "visit_date": "2026-08-09",
              "external_org_name": "省人民医院", "external_org_level": "province",
              "total_amount": 3000},
        headers=admin,
    )

    flow = client.get("/api/analytics/patient-flow", headers=admin).json()
    assert flow["inside_visits"] == 3
    assert flow["outside_visits"] == 2
    assert flow["county_visit_rate_pct"] == 60.0
    assert flow["outbound_rate_pct"] == 40.0
    assert flow["referred_outbound"] == 1
    assert flow["ordered_referral_rate_pct"] == 50.0
    assert flow["outside_by_level"] == {"city": 1, "province": 1}
    assert flow["outside_amount"] == 25000


def test_outbound_rejects_insurance_over_total(client, admin, setup):
    resp = client.post(
        "/api/analytics/outbound-visits",
        json={"patient_id": setup["patient"]["id"], "visit_date": "2026-08-10",
              "external_org_name": "某院", "total_amount": 100, "insurance_pay": 200},
        headers=admin,
    )
    assert resp.status_code == 422


def test_outbound_rejects_foreign_referral(client, admin, setup):
    """挂到别人的转诊单上会把有序转诊率算高，必须拦。"""
    other = client.post(
        "/api/patients", json={"name": "他人患者", "id_card": "331382199202022345"}, headers=admin
    ).json()
    referral = client.post(
        "/api/referrals",
        json={"patient_id": other["id"], "from_org_id": setup["town"]["id"],
              "to_org_id": setup["county"]["id"], "direction": "up", "reason": "他人转诊"},
        headers=admin,
    ).json()
    resp = client.post(
        "/api/analytics/outbound-visits",
        json={"patient_id": setup["patient"]["id"], "visit_date": "2026-08-11",
              "external_org_name": "某院", "referral_id": referral["id"]},
        headers=admin,
    )
    assert resp.status_code == 422


def test_outbound_filters(client, admin):
    inpatient = client.get("/api/analytics/outbound-visits?visit_type=inpatient", headers=admin).json()
    assert all(v["visit_type"] == "inpatient" for v in inpatient)
    province = client.get(
        "/api/analytics/outbound-visits?external_org_level=province", headers=admin
    ).json()
    assert all(v["external_org_level"] == "province" for v in province)


# ============================================================================
# T4.2 运行效率
# ============================================================================


@pytest.fixture(scope="module")
def inpatient_data(client, admin, setup):
    ward = client.post(
        "/api/inpatient/wards", json={"org_id": setup["county"]["id"], "name": "效率病区"}, headers=admin
    ).json()
    beds = [
        client.post(
            "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": f"E{i:02d}"}, headers=admin
        ).json()
        for i in range(4)
    ]
    for i, bed in enumerate(beds[:2]):
        patient = client.post(
            "/api/patients", json={"name": f"住院患者{i}", "id_card": f"33138219930303{i:04d}"},
            headers=admin,
        ).json()
        client.post(
            "/api/inpatient/admissions",
            json={"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"],
                  "doctor_name": "住院医师", "diagnosis_name": "肺炎"},
            headers=admin,
        )
    client.post(
        "/api/mgmt/employees",
        json={"org_id": setup["county"]["id"], "name": "张医师", "title": "中级",
              "position": "主治医师"},
        headers=admin,
    )
    client.post(
        "/api/mgmt/employees",
        json={"org_id": setup["county"]["id"], "name": "李会计", "title": "中级", "position": "会计"},
        headers=admin,
    )
    return {"ward": ward, "beds": beds}


def test_efficiency_metrics(client, admin, setup, inpatient_data):
    period = date.today().strftime("%Y-%m")
    rows = client.get(f"/api/analytics/efficiency?period={period}", headers=admin).json()
    county = next(r for r in rows if r["org_id"] == setup["county"]["id"])
    assert county["beds"] == 4
    assert county["occupied_bed_days"] >= 2
    # 只有"主治医师"计入医师数，会计不计
    assert county["doctors"] == 1
    assert county["bed_occupancy_rate_pct"] > 0


def test_efficiency_rejects_bad_period(client, admin):
    assert client.get("/api/analytics/efficiency?period=2026-8-1", headers=admin).status_code == 422


def test_avg_length_of_stay_counts_only_discharged_in_period(client, admin, setup, inpatient_data):
    """在院未出院的不计入平均住院日分母。"""
    period = date.today().strftime("%Y-%m")
    before = next(
        r for r in client.get(f"/api/analytics/efficiency?period={period}", headers=admin).json()
        if r["org_id"] == setup["county"]["id"]
    )
    assert before["discharges"] == 0
    assert before["avg_length_of_stay"] == 0.0

    admissions = client.get(
        f"/api/inpatient/admissions?ward_id={inpatient_data['ward']['id']}", headers=admin
    ).json()
    adm = admissions[0]
    client.post(
        f"/api/inpatient/admissions/{adm['id']}/case-summary",
        json={"discharge_diagnosis": "肺炎", "total_cost": 100, "drug_cost": 10, "outcome": "治愈"},
        headers=admin,
    )
    client.post(
        "/api/billing/settlements", json={"bill_type": "inpatient", "admission_id": adm["id"]},
        headers=admin,
    )
    client.post(f"/api/inpatient/admissions/{adm['id']}/discharge", headers=admin)

    after = next(
        r for r in client.get(f"/api/analytics/efficiency?period={period}", headers=admin).json()
        if r["org_id"] == setup["county"]["id"]
    )
    assert after["discharges"] == 1
    assert after["avg_length_of_stay"] >= 1  # 当日入当日出计 1 天，不是 0
    assert after["bed_turnover"] == round(1 / 4, 2)


# ============================================================================
# T4.3 自定义绩效公式与综合报告
# ============================================================================


def test_formula_variables_listed(client, admin):
    rows = client.get("/api/analytics/formula-variables", headers=admin).json()
    names = {r["name"] for r in rows}
    assert {"encounters", "referrals_up", "avg_length_of_stay", "doctors"} <= names
    assert all(r["description"] for r in rows)


def test_create_formula_validates_expression(client, admin):
    bad = client.post(
        "/api/analytics/formulas",
        json={"key": "bad", "name": "非法公式", "expression": "encounters + unknown_var"},
        headers=admin,
    )
    assert bad.status_code == 422
    assert "未知变量" in bad.json()["detail"]

    dangerous = client.post(
        "/api/analytics/formulas",
        json={"key": "evil", "name": "危险公式", "expression": "__import__('os').system('ls')"},
        headers=admin,
    )
    assert dangerous.status_code == 422


def test_formula_crud_and_report(client, admin, setup):
    created = client.post(
        "/api/analytics/formulas",
        json={"key": "up_rate", "name": "上转占比", "unit": "%",
              "expression": "round(referrals_up / encounters * 100, 2)", "weight": 40},
        headers=admin,
    )
    assert created.status_code == 201

    assert client.post(
        "/api/analytics/formulas",
        json={"key": "up_rate", "name": "重复", "expression": "encounters"},
        headers=admin,
    ).status_code == 409

    client.post(
        "/api/analytics/formulas",
        json={"key": "obs_only", "name": "仅观测诊疗量", "expression": "encounters", "weight": 0},
        headers=admin,
    )

    period = date.today().strftime("%Y-%m")
    report = client.get(f"/api/analytics/performance-report?period={period}", headers=admin).json()
    assert report["formula_count"] == 2
    town = next(o for o in report["orgs"] if o["org_id"] == setup["town"]["id"])
    keys = {i["key"] for i in town["items"]}
    assert keys == {"up_rate", "obs_only"}
    # weight=0 的公式只观测不计分
    obs = next(i for i in town["items"] if i["key"] == "obs_only")
    assert obs["value"] >= 3 and obs["weight"] == 0
    assert town["weighted_score"] == next(i for i in town["items"] if i["key"] == "up_rate")["value"]


def test_report_handles_zero_denominator_org(client, admin):
    """本期没有业务量的机构不该让整张报表报错。"""
    period = date.today().strftime("%Y-%m")
    report = client.get(f"/api/analytics/performance-report?period={period}", headers=admin).json()
    assert all(
        all(i["value"] is not None for i in org["items"]) for org in report["orgs"]
    )


def test_deactivate_formula_excludes_from_report(client, admin):
    assert client.delete("/api/analytics/formulas/obs_only", headers=admin).status_code == 200
    period = date.today().strftime("%Y-%m")
    report = client.get(f"/api/analytics/performance-report?period={period}", headers=admin).json()
    assert report["formula_count"] == 1
    # 停用不物理删除：历史报表还要能解释当时的口径
    assert any(f["key"] == "obs_only" and f["active"] is False
               for f in client.get("/api/analytics/formulas", headers=admin).json())


def test_formula_write_requires_admin(client, admin):
    client.post(
        "/api/users",
        json={"username": "ana_dir", "password": "passw0rd1", "full_name": "分析主任", "role": "director"},
        headers=admin,
    )
    token = client.post(
        "/api/auth/login", json={"username": "ana_dir", "password": "passw0rd1"}
    ).json()["access_token"]
    resp = client.post(
        "/api/analytics/formulas",
        json={"key": "x", "name": "x", "expression": "encounters"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_report_requires_director(client, admin):
    client.post(
        "/api/users",
        json={"username": "ana_doc", "password": "passw0rd1", "full_name": "分析医生", "role": "doctor"},
        headers=admin,
    )
    token = client.post(
        "/api/auth/login", json={"username": "ana_doc", "password": "passw0rd1"}
    ).json()["access_token"]
    period = date.today().strftime("%Y-%m")
    resp = client.get(
        f"/api/analytics/performance-report?period={period}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
