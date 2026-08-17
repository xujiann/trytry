"""S15 电子健康卡一卡通：发卡、展码、核身、绑定医保凭证、冻结。

守两条口径：
- 一患者一卡（重复发卡 409）；
- 核身按 token 反查、冻结后拒绝；
- 患者可见性把住 GET/{patient_id}（无关系的非全域账号 403）。

发卡/展码/核身/绑定/冻结统一用 admin（全域，assert_patient_visible 直接放行），
再单独用一个与患者无任何业务关系的 operator 证明可见性闸门确实生效。
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


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "健康卡镇卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def operator(client, admin, org):
    """挂在 org 上的经办：与测试患者无任何就诊/签约/转诊关系。"""
    client.post(
        "/api/users",
        json={"username": "ehc_op", "password": "passw0rd1", "role": "operator",
              "org_id": org["id"]},
        headers=admin,
    )
    resp = client.post("/api/auth/login", json={"username": "ehc_op", "password": "passw0rd1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def patient(client, admin):
    return client.post(
        "/api/patients",
        json={"name": "健康卡患者", "id_card": "330102199003074567"},
        headers=admin,
    ).json()


# ---------------- 发卡 ----------------


def test_发卡返回卡号且重复发卡409(client, admin, patient):
    resp = client.post("/api/ehealth-cards", json={"patient_id": patient["id"]}, headers=admin)
    assert resp.status_code == 201, resp.text
    card = resp.json()
    assert card["card_no"].startswith("EHC")
    assert card["qr_token"] and card["status"] == "active"
    assert card["patient_id"] == patient["id"]

    dup = client.post("/api/ehealth-cards", json={"patient_id": patient["id"]}, headers=admin)
    assert dup.status_code == 409


def test_发卡患者不存在404(client, admin):
    resp = client.post("/api/ehealth-cards", json={"patient_id": 999999}, headers=admin)
    assert resp.status_code == 404


# ---------------- 可见性 ----------------


def test_无关系账号查卡被拦(client, operator, patient):
    """operator 挂在与该患者无关的机构，GET/{patient_id} 应 403。"""
    resp = client.get(f"/api/ehealth-cards/{patient['id']}", headers=operator)
    assert resp.status_code == 403


def test_查卡返回卡信息(client, admin, patient):
    resp = client.get(f"/api/ehealth-cards/{patient['id']}", headers=admin)
    assert resp.status_code == 200
    assert resp.json()["card_no"].startswith("EHC")


# ---------------- 展码与核身 ----------------


@pytest.fixture(scope="module")
def qr_token(client, admin, patient):
    resp = client.post(f"/api/ehealth-cards/{patient['id']}/show-qr", headers=admin)
    assert resp.status_code == 200
    body = resp.json()
    assert body["patient_id"] == patient["id"]
    return body["qr_token"]


def test_核身按token反查患者(client, admin, patient, qr_token):
    resp = client.post("/api/ehealth-cards/verify", json={"qr_token": qr_token}, headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["patient_id"] == patient["id"]
    assert body["patient"]["ehc_no"] == patient["ehc_no"]
    assert body["patient"]["name"] == "健康卡患者"


def test_核身未知凭证404(client, admin):
    resp = client.post("/api/ehealth-cards/verify",
                       json={"qr_token": "deadbeef" * 8}, headers=admin)
    assert resp.status_code == 404


# ---------------- 绑定医保凭证 ----------------


def test_绑定医保凭证(client, admin, patient):
    resp = client.patch(f"/api/ehealth-cards/{patient['id']}/bind-voucher",
                        json={"medical_voucher": "YB2026001122"}, headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["medical_voucher"] == "YB2026001122"
    again = client.get(f"/api/ehealth-cards/{patient['id']}", headers=admin)
    assert again.json()["medical_voucher"] == "YB2026001122"


# ---------------- 冻结 ----------------


def test_冻结后核身返回409(client, admin, patient, qr_token):
    frozen = client.patch(f"/api/ehealth-cards/{patient['id']}/freeze", headers=admin)
    assert frozen.status_code == 200 and frozen.json()["status"] == "frozen"

    resp = client.post("/api/ehealth-cards/verify", json={"qr_token": qr_token}, headers=admin)
    assert resp.status_code == 409
    assert "冻结" in resp.json()["detail"]

    # 解冻后可再核身
    client.patch(f"/api/ehealth-cards/{patient['id']}/unfreeze", headers=admin)
    ok = client.post("/api/ehealth-cards/verify", json={"qr_token": qr_token}, headers=admin)
    assert ok.status_code == 200
