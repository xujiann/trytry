"""决策指标扩展 `/api/analytics` 十个端点的**特征化网 + 响应契约**。

套路同 `test_metrics_contract.py`：先补网钉住键集合与类型 → 再加 `response_model`
→ 加完逐字节不变（CLAUDE.md §11）。

**本簇最要紧的一条判断：`round()` 与 Money 列派生的数值一律 `int | float`。**
这不是保守，是实测出来的——同一个 `total_amount` 字段，一行返回 `1234.5`（float）、
另一行返回 `100`（**int**）：`Money` 是 `Numeric(14,2, asdecimal=False)`，整数值读回来
就是 int；`round(x, 2)` 对整数入参同样返回 int（`cssd.total_cost` 早有前例）。
声明成 `float` 会把 `100` 变 `100.0`——改字节。Pydantic 的智能联合对 `int | float`
原样保留输入类型，是这里唯一字节安全的写法。

比率字段（`*_pct`）另说：`patient-flow` 的三个比率两条分支都走浮点，实测恒为 float。

另一处多态：`performance-report` 的 `orgs[].items` —— 正常项是
`{key,name,unit,weight,value}`，公式求值失败时 `value` 为 `None` 且**多出 `error` 键**。
逐字段建模就得声明 `error`，那会给成功项注入 `"error": null`，同样是改字节，
故与 `metrics.drilldown.items` 一样用宽字典。
"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import Encounter, Prescription, PrescriptionItem, Referral, User


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def seeded(client, admin):
    """一条**小数**金额 + 一条**整数**金额——两条都要，int/float 的区分全靠它。"""
    org = client.post("/api/organizations",
                      json={"name": "分析契约院", "org_type": "township", "level": "township"},
                      headers=admin).json()
    patient = client.post("/api/patients",
                          json={"name": "分析契约患者", "id_card": "330281199012124514"},
                          headers=admin).json()
    with SessionLocal() as db:
        doctor = db.query(User).filter(User.username == "admin").first()
        db.add(Encounter(patient_id=patient["id"], org_id=org["id"], encounter_type="outpatient",
                         diagnosis_name="高血压", diagnosis_code="I10", doctor_name="张医生",
                         created_at=datetime(2026, 8, 1, 9, 0, 0)))
        db.add(Referral(patient_id=patient["id"], from_org_id=org["id"], to_org_id=org["id"],
                        direction="up", reason="上转", status="completed", created_by=doctor.id))
        rx = Prescription(patient_id=patient["id"], org_id=org["id"], diagnosis_name="高血压",
                          status="auto_passed", created_by=doctor.id,
                          created_at=datetime(2026, 8, 1, 9, 0, 0))
        db.add(rx)
        db.flush()
        db.add(PrescriptionItem(prescription_id=rx.id, drug_code="AC-1", drug_name="氨氯地平",
                                daily_dose=5.0, days=30))
        db.commit()
    for amount, pay, day in ((1234.5, 800, "2026-08-01"), (100, 0, "2026-08-02")):
        resp = client.post("/api/analytics/outbound-visits", json={
            "patient_id": patient["id"], "visit_date": day, "external_org_name": "市一院",
            "external_org_level": "city", "visit_type": "outpatient",
            "total_amount": amount, "insurance_pay": pay}, headers=admin)
        assert resp.status_code == 201, resp.text
    client.post("/api/analytics/formulas", json={
        "key": "ct_f", "name": "契约公式", "expression": "referrals_up",
        "unit": "次", "weight": 1.0}, headers=admin)
    return {"org_id": org["id"], "patient_id": patient["id"]}


def _get(client, admin, url):
    resp = client.get(url, headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _num(value) -> bool:
    """数值且不是 bool（bool 是 int 的子类，不当心会漏判）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ----------------------------------------------------- /outbound-visits
OUTBOUND_KEYS = {
    "id", "patient_id", "patient_name", "visit_date", "external_org_name",
    "external_org_level", "visit_type", "diagnosis_name", "total_amount",
    "insurance_pay", "referral_id", "referred", "source",
}


def test_outbound_列表键集合与类型(client, admin, seeded):
    rows = _get(client, admin, "/api/analytics/outbound-visits")
    assert rows, "seeded 造了两条，空列表什么都钉不住"
    for row in rows:
        assert set(row) == OUTBOUND_KEYS
        assert isinstance(row["referred"], bool)
        assert row["referral_id"] is None or isinstance(row["referral_id"], int)
        assert _num(row["total_amount"]) and _num(row["insurance_pay"])


def test_金额字段的int与float都真实出现过(client, admin, seeded):
    """本簇建模的**全部依据**：同一字段两种数值类型并存。

    这条要是绿不了（比如以后 Money 的读法变了），`int | float` 的写法就该重新审视——
    但在那之前，把 `total_amount` 声明成 `float` 会把 `100` 变成 `100.0`，是改字节。
    """
    rows = _get(client, admin, "/api/analytics/outbound-visits")
    kinds = {type(r["total_amount"]).__name__ for r in rows}
    assert kinds == {"int", "float"}, (
        f"实测到的金额类型是 {kinds}——seeded 特意造了 1234.5 与 100 两条，"
        "两种类型都要出现，否则本簇 int|float 的建模依据就不成立"
    )
    assert any(isinstance(r["insurance_pay"], int) for r in rows)


def test_outbound_新建返回与列表同形(client, admin, seeded):
    resp = client.post("/api/analytics/outbound-visits", json={
        "patient_id": seeded["patient_id"], "visit_date": "2026-08-03",
        "external_org_name": "省院", "external_org_level": "province",
        "visit_type": "inpatient", "total_amount": 200, "insurance_pay": 50}, headers=admin)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == OUTBOUND_KEYS
    assert isinstance(body["total_amount"], int), "整数金额被转成 float 了（改字节）"


# --------------------------------------------------------- /patient-flow
FLOW_KEYS = {
    "inside_visits", "outside_visits", "total_visits", "county_visit_rate_pct",
    "outbound_rate_pct", "referred_outbound", "ordered_referral_rate_pct",
    "outside_by_level", "outside_amount",
}
FLOW_PCT = ["county_visit_rate_pct", "outbound_rate_pct", "ordered_referral_rate_pct"]


def test_patient_flow_键集合与类型(client, admin, seeded):
    body = _get(client, admin, "/api/analytics/patient-flow")
    assert set(body) == FLOW_KEYS
    for key in ("inside_visits", "outside_visits", "total_visits", "referred_outbound"):
        assert isinstance(body[key], int), f"{key} 不是 int"
    for key in FLOW_PCT:
        assert isinstance(body[key], float), f"{key} 不是 float"
    assert isinstance(body["outside_by_level"], dict)
    assert _num(body["outside_amount"])


def test_patient_flow_空窗口下金额是int(client, admin, seeded):
    """空窗口是 Money 求和的零分支——实测返回 `0`（int）而不是 `0.0`。"""
    body = _get(client, admin,
                "/api/analytics/patient-flow?start=2000-01-01&end=2000-01-02")
    assert body["outside_amount"] == 0
    assert isinstance(body["outside_amount"], int), "空窗口金额被转成 float 了（改字节）"
    for key in FLOW_PCT:
        assert isinstance(body[key], float), f"{key} 在零分支上不是 float"


# ----------------------------------------------------------- /efficiency
EFFICIENCY_KEYS = {
    "org_id", "org_name", "beds", "discharges", "occupied_bed_days",
    "avg_length_of_stay", "bed_turnover", "bed_occupancy_rate_pct",
    "visits", "doctors", "visits_per_doctor_per_day",
}


def test_efficiency_键集合与类型(client, admin, seeded):
    rows = _get(client, admin, "/api/analytics/efficiency?period=2026-08")
    assert rows, "至少有一家机构"
    for row in rows:
        assert set(row) == EFFICIENCY_KEYS
        for key in ("org_id", "beds", "discharges", "visits", "doctors"):
            assert isinstance(row[key], int), f"{key} 不是 int"
        for key in ("occupied_bed_days", "avg_length_of_stay", "bed_turnover",
                    "bed_occupancy_rate_pct", "visits_per_doctor_per_day"):
            assert _num(row[key]), f"{key} 不是数值"


def test_efficiency_非法期间422(client, admin, seeded):
    assert client.get("/api/analytics/efficiency?period=202608",
                      headers=admin).status_code == 422


# ------------------------------------------------------------- /formulas
def test_formula_variables_键集合(client, admin, seeded):
    rows = _get(client, admin, "/api/analytics/formula-variables")
    assert rows
    for row in rows:
        assert set(row) == {"name", "description"}


FORMULA_KEYS = {"id", "key", "name", "expression", "unit", "higher_is_better", "weight", "active"}


def test_formulas_列表键集合与类型(client, admin, seeded):
    rows = _get(client, admin, "/api/analytics/formulas")
    assert rows, "seeded 建了一条公式"
    for row in rows:
        assert set(row) == FORMULA_KEYS
        assert isinstance(row["higher_is_better"], bool) and isinstance(row["active"], bool)
        assert _num(row["weight"])


def test_停用公式的返回形状(client, admin, seeded):
    client.post("/api/analytics/formulas", json={
        "key": "tmp_off", "name": "待停用", "expression": "referrals_up",
        "unit": "次", "weight": 0}, headers=admin)
    resp = client.delete("/api/analytics/formulas/tmp_off", headers=admin)
    assert resp.status_code == 200
    assert resp.json() == {"key": "tmp_off", "active": False}


# --------------------------------------------------- /performance-report
def test_performance_report_结构(client, admin, seeded):
    body = _get(client, admin, "/api/analytics/performance-report?period=2026-08")
    assert set(body) == {"period", "formula_count", "orgs"}
    assert isinstance(body["formula_count"], int)
    assert body["orgs"], "至少一家机构"
    for org in body["orgs"]:
        assert set(org) == {"org_id", "org_name", "level", "items", "weighted_score"}
        assert _num(org["weighted_score"])
        assert org["items"], "seeded 建了公式，items 不该为空"


def test_performance_report_的item是多态的_成功项没有error键(client, admin, seeded):
    """成功项**不带** `error` 键——这正是 `items` 不能逐字段建模的原因。

    Pydantic 逐字段建模就得声明 `error`，那会给每个成功项注入 `"error": null`，
    是改字节。这条把"成功项没有 error 键"钉死，防止有人"顺手补全"成固定模型。
    """
    body = _get(client, admin, "/api/analytics/performance-report?period=2026-08")
    items = body["orgs"][0]["items"]
    for item in items:
        assert {"key", "name", "unit", "weight"} <= set(item)
        if item.get("value") is not None:
            assert "error" not in item, "成功项多出了 error 键——契约把它注进来了"


# ------------------------------------------------------------- /drug-use
DRUG_ORG_KEYS = {
    "org_id", "org_name", "inpatient_total", "inpatient_drug", "inpatient_drug_ratio_pct",
    "outpatient_total", "outpatient_drug", "outpatient_drug_ratio_pct", "antibiotic_ddds",
    "bed_days", "antibiotic_intensity", "ddd_uncovered_items", "intensity_unstable",
}


def test_drug_use_结构与类型(client, admin, seeded):
    body = _get(client, admin, "/api/analytics/drug-use?period=2026-08")
    assert set(body) == {"period", "caliber", "warnings", "orgs"}
    assert set(body["caliber"]) == {"drug_ratio", "antibiotic_intensity"}
    assert all(isinstance(w, str) for w in body["warnings"])
    assert body["orgs"]
    for row in body["orgs"]:
        assert set(row) == DRUG_ORG_KEYS
        assert isinstance(row["bed_days"], int)
        assert isinstance(row["ddd_uncovered_items"], int)
        assert isinstance(row["intensity_unstable"], bool)
        for key in ("inpatient_total", "inpatient_drug", "antibiotic_ddds",
                    "antibiotic_intensity"):
            assert _num(row[key]), f"{key} 不是数值"


def test_drug_use_口径说明是常量文案(client, admin, seeded):
    """`caliber` 两段是写死的口径解释，前端直接显示——不该随数据变。"""
    a = _get(client, admin, "/api/analytics/drug-use?period=2026-08")["caliber"]
    b = _get(client, admin, "/api/analytics/drug-use?period=2026-07")["caliber"]
    assert a == b and all(isinstance(v, str) and v for v in a.values())
