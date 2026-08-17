"""E6 数据智能层：预测 / 慢病风险分层 / DRG 费用离群 / 权重配置。

守的口径：
- 预测在样本不足时明说"样本不足"而非硬编一条趋势；有历史时按月序列外推；
- 风险分层未配置权重时 409（不静默给"人人低危"）；权重现算、high 建议上转；
- 横向可见范围：非全域账号的预测只统计到本机构，看不到别家的量。
"""
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

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
def orgs(client, admin):
    return [
        client.post(
            "/api/organizations",
            json={"name": name, "org_type": org_type, "level": level},
            headers=admin,
        ).json()
        for name, org_type, level in [
            ("智能县医院", "lead_hospital", "county"),
            ("智能东镇卫生院", "township", "township"),
        ]
    ]


@pytest.fixture(scope="module")
def operator(client, admin, orgs):
    """本机构（orgs[0]）操作员：非全域，只应看到本机构的量。"""
    client.post(
        "/api/users",
        json={"username": "intel_op", "password": "passw0rd1", "role": "operator",
              "org_id": orgs[0]["id"]},
        headers=admin,
    )
    resp = client.post("/api/auth/login", json={"username": "intel_op", "password": "passw0rd1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _month_back(n: int) -> datetime:
    """当前月往回数 n 个月的 15 号中午（用于回填 created_at）。"""
    today = date.today()
    y, m = today.year, today.month
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return datetime(y, m, 15, 12, 0, 0)


def _sum_history(payload: dict) -> float:
    return sum(p["value"] for p in payload["history"])


# ---------------- 慢病风险分层 ----------------


def test_未配置风险权重返回409(client, admin):
    """没有 active 权重时必须 409，避免"人人 0 分低危"这种无意义结论。

    本用例刻意排在最前、且不使用 weights fixture，保证此刻权重表为空。
    """
    patient = client.post(
        "/api/patients",
        json={"name": "空权重患者", "id_card": "331782196001011234"},
        headers=admin,
    ).json()
    resp = client.get(f"/api/intel/chronic-risk/{patient['id']}", headers=admin)
    assert resp.status_code == 409
    assert "未配置" in resp.json()["detail"]


@pytest.fixture(scope="module")
def weights(client, admin):
    """配置五因子权重（bp/glucose 各 3，complication 2，age_65/adherence 各 1）。"""
    config = [
        ("bp_uncontrolled", 3.0),
        ("glucose_uncontrolled", 3.0),
        ("complication", 2.0),
        ("age_65", 1.0),
        ("poor_adherence", 1.0),
    ]
    for factor, weight in config:
        resp = client.post(
            "/api/intel/risk-weights",
            json={"factor": factor, "weight": weight, "active": True},
            headers=admin,
        )
        assert resp.status_code == 200, resp.text
    return config


def test_权重upsert幂等且可列出(client, admin, weights):
    """同 factor 再 POST 是更新不是报错（幂等 upsert）。"""
    resp = client.post(
        "/api/intel/risk-weights",
        json={"factor": "bp_uncontrolled", "weight": 5.0, "active": True},
        headers=admin,
    )
    assert resp.status_code == 200
    assert resp.json()["weight"] == 5.0
    rows = client.get("/api/intel/risk-weights", headers=admin).json()
    factors = {r["factor"]: r["weight"] for r in rows}
    assert factors["bp_uncontrolled"] == 5.0  # 覆盖而非新增
    assert len([r for r in rows if r["factor"] == "bp_uncontrolled"]) == 1


def test_权重配置限管理员(client, operator):
    resp = client.post(
        "/api/intel/risk-weights",
        json={"factor": "age_65", "weight": 9.0},
        headers=operator,
    )
    assert resp.status_code == 403


@pytest.fixture(scope="module")
def high_risk_patient(client, admin, orgs, weights):
    """造一个多因子全中的高危患者：76 岁、血压/血糖双失控、依从性差、3 级档案。"""
    patient = client.post(
        "/api/patients",
        json={"name": "高危老人", "id_card": "331782195001011234",
              "birth_date": "1950-01-01"},
        headers=admin,
    ).json()
    chronic = client.post(
        "/api/chronic",
        json={"patient_id": patient["id"], "disease": "hypertension",
              "managed_by_org_id": orgs[0]["id"]},
        headers=admin,
    ).json()
    # 随访：SBP 165 / 血糖 11 / 依从性 2（0~10，越低越差）→ 触发多因子且升到 3 级
    client.post(
        f"/api/chronic/{chronic['id']}/followups",
        json={"sbp": 165, "dbp": 100, "glucose": 11.0,
              "metrics": {"adherence_score": 2}},
        headers=admin,
    )
    return patient


def test_多因子高危建议上转(client, admin, high_risk_patient):
    resp = client.get(f"/api/intel/chronic-risk/{high_risk_patient['id']}", headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["level"] == "high"
    assert body["refer_up_suggested"] is True
    triggered = {f["factor"]: f["triggered"] for f in body["factors"]}
    assert triggered["bp_uncontrolled"] is True
    assert triggered["glucose_uncontrolled"] is True
    assert triggered["age_65"] is True
    assert triggered["poor_adherence"] is True
    # score 是被触发因子权重之和，随比例定档
    assert body["score"] > 0
    assert 0.0 <= body["ratio"] <= 1.0


def test_无随访患者低危(client, admin, orgs, weights):
    """只有年龄一个因子命中的年轻无随访患者应为低危、不建议上转。"""
    patient = client.post(
        "/api/patients",
        json={"name": "年轻无慢病", "id_card": "331782200001011234",
              "birth_date": "2000-01-01"},
        headers=admin,
    ).json()
    body = client.get(f"/api/intel/chronic-risk/{patient['id']}", headers=admin).json()
    assert body["level"] == "low"
    assert body["refer_up_suggested"] is False


# ---------------- 业务量预测 ----------------


def test_空库预测样本不足(client, admin):
    """指定一个不会有历史的指标区间：非零月不足 3 个时不外推，明说样本不足。"""
    body = client.get(
        "/api/intel/forecast", params={"metric": "referrals", "months": 6}, headers=admin
    ).json()
    assert body["forecast"] == []
    assert body["note"] == "样本不足"
    assert len(body["history"]) == 6
    assert all("month" in p and "value" in p for p in body["history"])


def test_未知指标422(client, admin):
    resp = client.get("/api/intel/forecast", params={"metric": "unknown"}, headers=admin)
    assert resp.status_code == 422


@pytest.fixture(scope="module")
def seeded_encounters(client, admin, orgs):
    """回填 6 个月、每月 1 笔本机构就诊 + 本月额外几笔他机构就诊。

    通过 API 无法回填 created_at，直接落库；FK 依赖的患者/机构先经 API 建好。
    """
    from app.database import SessionLocal
    from app.models import Encounter

    patient = client.post(
        "/api/patients",
        json={"name": "就诊序列患者", "id_card": "331782197001011234"},
        headers=admin,
    ).json()
    db = SessionLocal()
    try:
        # orgs[0]：近 6 个月每月 1 笔（6 个非零月 → 可外推）
        for n in range(6):
            db.add(Encounter(patient_id=patient["id"], org_id=orgs[0]["id"],
                             encounter_type="outpatient", created_at=_month_back(n)))
        # orgs[1]：仅本月 3 笔（只有全域账号看得到）
        for _ in range(3):
            db.add(Encounter(patient_id=patient["id"], org_id=orgs[1]["id"],
                             encounter_type="outpatient", created_at=_month_back(0)))
        db.commit()
    finally:
        db.close()
    return {"org0": orgs[0]["id"], "org1": orgs[1]["id"]}


def test_有历史时按月序列外推(client, admin, seeded_encounters):
    body = client.get(
        "/api/intel/forecast",
        params={"metric": "encounters", "months": 6, "horizon": 3},
        headers=admin,
    ).json()
    assert body["note"] != "样本不足"
    assert len(body["forecast"]) == 3
    assert all(p["value"] >= 0 for p in body["forecast"])
    assert all("month" in p and "value" in p for p in body["forecast"])
    assert "slope" in body
    assert len(body["moving_average"]) == len(body["history"])


def test_非全域账号只统计本机构(client, admin, operator, seeded_encounters):
    """orgs[0] 的操作员看不到 orgs[1] 的本月就诊：合计应小于全域合计。"""
    admin_body = client.get(
        "/api/intel/forecast", params={"metric": "encounters", "months": 6}, headers=admin
    ).json()
    op_body = client.get(
        "/api/intel/forecast", params={"metric": "encounters", "months": 6}, headers=operator
    ).json()
    admin_total = _sum_history(admin_body)
    op_total = _sum_history(op_body)
    assert op_total == 6              # 仅 orgs[0] 的 6 笔
    assert admin_total >= 9           # orgs[0] 6 笔 + orgs[1] 3 笔
    assert op_total < admin_total


# ---------------- DRG 费用离群 ----------------


def test_drg离群结构与样本门槛(client, admin):
    """空库（无病案）时返回结构完整、分组为空——不报错、不臆造离群。"""
    body = client.get("/api/intel/anomaly/drg", params={"multiplier": 2}, headers=admin).json()
    assert "groups" in body and "insufficient_groups" in body
    assert body["multiplier"] == 2
    assert body["min_cases"] == 5
