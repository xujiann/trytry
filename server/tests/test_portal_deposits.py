"""居民端押金透出（P1-24b）：本人可见、他人 404、未绑定 403，余额口径与 billing 一致。

余额复用 billing.deposit_balance 的流水现算口径（预交-退费-冲抵）——
居民端另算一套只会造出第二个数字。仅限本人住院（不含家属代查）。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app
from app.models import SmsCode
from app.routers.portal import _reset_portal_failures


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_state():
    _reset_portal_failures()
    yield
    _reset_portal_failures()


@pytest.fixture(scope="module")
def ward(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "押金透出县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    return client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "透出一病区"}, headers=admin
    ).json()


def _admit(client, admin, ward, name, id_card, phone, bed_no):
    patient = client.post(
        "/api/patients",
        json={"name": name, "id_card": id_card, "phone": phone},
        headers=admin,
    ).json()
    bed = client.post(
        "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": bed_no}, headers=admin
    ).json()
    admission = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"]},
        headers=admin,
    ).json()
    return patient, admission


def _resident_login(client, phone) -> dict:
    """验证码登录；手机号唯一命中患者档案时自动实名绑定。"""
    from app.database import SessionLocal

    with SessionLocal() as db:  # 清冷却，允许连续下发
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()
    code = client.post(
        "/api/portal/auth/sms/code", json={"phone": phone, "purpose": "login"}
    ).json()["debug_code"]
    body = client.post(
        "/api/portal/auth/sms/login", json={"phone": phone, "code": code}
    ).json()
    return {"Authorization": f"Bearer {body['access_token']}"}


@pytest.fixture(scope="module")
def mine(client, admin, ward):
    """本人：患者档案 + 住院 + 两笔押金（预交 500、退 100 → 余额 400）。"""
    patient, admission = _admit(
        client, admin, ward, "押金本人", "330782199003030011", "13800031001", "T-01"
    )
    for payload in (
        ("/api/billing/deposits", {"admission_id": admission["id"], "amount": 500}),
        ("/api/billing/deposits/refund", {"admission_id": admission["id"], "amount": 100}),
    ):
        resp = client.post(payload[0], json=payload[1], headers=admin)
        assert resp.status_code in (200, 201), resp.text
    return {"patient": patient, "admission": admission,
            "headers": _resident_login(client, "13800031001")}


@pytest.fixture(scope="module")
def other_admission(client, admin, ward):
    _, admission = _admit(
        client, admin, ward, "押金他人", "330782199003030022", "13800031002", "T-02"
    )
    return admission


def test_本人押金流水与余额(client, mine):
    resp = client.get(
        "/api/portal/me/deposits",
        params={"admission_id": mine["admission"]["id"]},
        headers=mine["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["admission_id"] == mine["admission"]["id"]
    assert body["balance"] == 400.0  # 与 billing 流水现算口径一致：500 预交 - 100 退费
    assert [(i["deposit_type"], i["amount"]) for i in body["items"]] == [
        ("refund", 100.0), ("prepay", 500.0)
    ]
    # 经办人是院内信息，不透出给居民端
    assert all("operator" not in i for i in body["items"])


def test_他人住院404(client, mine, other_admission):
    resp = client.get(
        "/api/portal/me/deposits",
        params={"admission_id": other_admission["id"]},
        headers=mine["headers"],
    )
    assert resp.status_code == 404, "越权与不存在同按 404，不区分'不是你的'与'没有'"
    assert client.get(
        "/api/portal/me/deposits", params={"admission_id": 999999}, headers=mine["headers"]
    ).status_code == 404


def test_未实名绑定403(client, mine):
    unbound = _resident_login(client, "13800039999")  # 无患者档案命中，不会自动绑定
    resp = client.get(
        "/api/portal/me/deposits",
        params={"admission_id": mine["admission"]["id"]},
        headers=unbound,
    )
    assert resp.status_code == 403, resp.text


def test_费用清单附押金余额(client, mine):
    resp = client.get(
        f"/api/portal/me/admissions/{mine['admission']['id']}/bill", headers=mine["headers"]
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deposit_balance"] == 400.0
