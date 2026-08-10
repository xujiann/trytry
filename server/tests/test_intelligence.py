"""M8 智能化：相互作用审方、慢病风险评分、采购建议。"""
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
def admin_headers(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def setup(client, admin_headers):
    org = client.post(
        "/api/organizations",
        json={"name": "智能化测试医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin_headers,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "智能化患者", "id_card": "320981198801017777"},
        headers=admin_headers,
    ).json()
    return {"org": org, "patient": patient}


def test_interaction_pair_forces_pharmacist_review(client, admin_headers, setup):
    # 规则：IX01 与 IX02 存在相互作用
    rule = client.post(
        "/api/prescriptions/rules",
        json={"drug_code": "IX01", "max_daily_dose": 100, "interactions": "IX02,IX09"},
        headers=admin_headers,
    )
    assert rule.status_code == 201
    assert rule.json()["interactions"] == "IX02,IX09"

    # 同方含冲突药对 → 转药师审并注明
    rx = client.post(
        "/api/prescriptions",
        json={
            "patient_id": setup["patient"]["id"],
            "org_id": setup["org"]["id"],
            "diagnosis_name": "感染",
            "items": [
                {"drug_code": "IX01", "drug_name": "华法林", "daily_dose": 5},
                {"drug_code": "IX02", "drug_name": "阿司匹林", "daily_dose": 100},
            ],
        },
        headers=admin_headers,
    ).json()
    assert rx["status"] == "pending_review"
    assert "相互作用" in rx["review_comment"]
    assert "华法林" in rx["review_comment"] and "阿司匹林" in rx["review_comment"]

    # 单独开具其中一种 → 系统审通过
    solo = client.post(
        "/api/prescriptions",
        json={
            "patient_id": setup["patient"]["id"],
            "org_id": setup["org"]["id"],
            "items": [{"drug_code": "IX01", "drug_name": "华法林", "daily_dose": 5}],
        },
        headers=admin_headers,
    ).json()
    assert solo["status"] == "auto_passed"


def test_chronic_risk_score(client, admin_headers, setup):
    chronic = client.post(
        "/api/chronic",
        json={
            "patient_id": setup["patient"]["id"],
            "disease": "hypertension",
            "managed_by_org_id": setup["org"]["id"],
        },
        headers=admin_headers,
    ).json()
    cid = chronic["id"]

    # 随访不足2次 → 数据不足，仅按分级基础分
    empty = client.get(f"/api/chronic/{cid}/risk", headers=admin_headers).json()
    assert empty["trend"] == "insufficient_data"
    assert empty["score"] == 20  # 1级基础分

    # 3次随访血压持续上升且达高危 → 3级基础分80 + 上升15 = 95
    for sbp, dbp in [(138, 88), (150, 95), (168, 102)]:
        client.post(
            f"/api/chronic/{cid}/followups",
            json={"sbp": sbp, "dbp": dbp},
            headers=admin_headers,
        )
    risk = client.get(f"/api/chronic/{cid}/risk", headers=admin_headers).json()
    assert risk["metric"] == "sbp"
    assert risk["recent_values"] == [138, 150, 168]
    assert risk["trend"] == "rising"
    assert risk["level"] == 3
    assert risk["score"] == 95
    assert risk["risk_level"] == "high"
    assert risk["refer_up_suggested"] is True

    assert client.get("/api/chronic/99999/risk", headers=admin_headers).status_code == 404


def test_purchase_suggestions(client, admin_headers, setup):
    # 近30天用药：IXBUY 5mg×30天=150；库存仅 40 → 建议采购 110
    client.post(
        "/api/prescriptions",
        json={
            "patient_id": setup["patient"]["id"],
            "org_id": setup["org"]["id"],
            "diagnosis_name": "慢病长期用药",
            "items": [{"drug_code": "IXBUY", "drug_name": "缬沙坦", "daily_dose": 5, "days": 30}],
        },
        headers=admin_headers,
    )
    client.post(
        "/api/pharmacy/stocks",
        json={
            "org_id": setup["org"]["id"],
            "drug_code": "IXBUY",
            "drug_name": "缬沙坦",
            "quantity": 40,
            "threshold": 10,
        },
        headers=admin_headers,
    )
    # 库存充足品种：不应出现在建议中
    client.post(
        "/api/pharmacy/stocks",
        json={
            "org_id": setup["org"]["id"],
            "drug_code": "IXFULL",
            "drug_name": "库存充足药",
            "quantity": 999,
            "threshold": 10,
        },
        headers=admin_headers,
    )

    suggestions = client.get("/api/pharmacy/purchase-suggestions", headers=admin_headers).json()
    codes = {s["drug_code"]: s for s in suggestions}
    assert "IXBUY" in codes
    assert codes["IXBUY"]["usage_30d"] == 150.0
    assert codes["IXBUY"]["current_stock"] == 40
    assert codes["IXBUY"]["suggested_quantity"] == 110
    assert "IXFULL" not in codes
