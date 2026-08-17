"""S7 在线复诊/图文咨询——医方应答端。

居民端建单（status=open）后，医方侧：本院医生回复→replied、结束→closed；
跨机构医生回复→403（A 案 by-id 写隔离，用 doctor 而非全域的 director）；
对已结束单再回复→409。
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


def _login(client, u, p):
    tok = client.post("/api/auth/login", json={"username": u, "password": p}).json()
    return {"Authorization": f"Bearer {tok['access_token']}"}


def _send_code(client, phone):
    from app.database import SessionLocal
    from app.models import SmsCode
    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()
    return client.post(
        "/api/portal/auth/sms/code", json={"phone": phone, "purpose": "login"}
    ).json()["debug_code"]


def _resident(client, phone, name, id_card):
    code = _send_code(client, phone)
    body = client.post(
        "/api/portal/auth/sms/login", json={"phone": phone, "code": code}
    ).json()
    h = {"Authorization": f"Bearer {body['access_token']}"}
    client.post("/api/portal/auth/realname", json={"name": name, "id_card": id_card}, headers=h)
    return h


@pytest.fixture(scope="module")
def admin(client):
    return _login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def setup(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "复诊卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    org2 = client.post(
        "/api/organizations",
        json={"name": "复诊他院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    client.post(
        "/api/users",
        json={"username": "s7_doc", "password": "pw123456", "full_name": "复诊医生",
              "role": "doctor", "org_id": org["id"]},
        headers=admin,
    )
    client.post(
        "/api/users",
        json={"username": "s7_doc2", "password": "pw123456", "full_name": "他院医生",
              "role": "doctor", "org_id": org2["id"]},
        headers=admin,
    )
    client.post(
        "/api/patients",
        json={"name": "复诊居民", "id_card": "330782199001019876", "phone": "13800770011"},
        headers=admin,
    )
    r1 = _resident(client, "13800770011", "复诊居民", "330782199001019876")
    return {"org": org, "org2": org2, "r1": r1,
            "doc": _login(client, "s7_doc", "pw123456"),
            "doc2": _login(client, "s7_doc2", "pw123456")}


def _new_consult(client, setup):
    r = client.post(
        "/api/portal/me/online-consults",
        json={"org_id": setup["org"]["id"], "question": "血压偏高怎么办"},
        headers=setup["r1"],
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_reply_and_close(client, setup):
    cid = _new_consult(client, setup)
    # 本院医生回复 → 200, status=replied，doctor_name 缺省取 full_name
    r = client.patch(
        f"/api/online-consults/{cid}/reply",
        json={"reply": "建议低盐饮食并复测"},
        headers=setup["doc"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "replied"
    assert r.json()["doctor_name"] == "复诊医生"
    # 结束 → 200, status=closed
    c = client.patch(f"/api/online-consults/{cid}/close", headers=setup["doc"])
    assert c.status_code == 200, c.text
    assert c.json()["status"] == "closed"


def test_cross_org_doctor_forbidden(client, setup):
    cid = _new_consult(client, setup)
    # 他院医生（doctor，非全域 director）回复本院单 → 403
    r = client.patch(
        f"/api/online-consults/{cid}/reply",
        json={"reply": "越权回复"},
        headers=setup["doc2"],
    )
    assert r.status_code == 403, r.text


def test_reply_to_closed_conflict(client, setup):
    cid = _new_consult(client, setup)
    client.patch(f"/api/online-consults/{cid}/reply", json={"reply": "先答一句"}, headers=setup["doc"])
    client.patch(f"/api/online-consults/{cid}/close", headers=setup["doc"])
    # 已结束单再回复 → 409
    r = client.patch(
        f"/api/online-consults/{cid}/reply",
        json={"reply": "再答"},
        headers=setup["doc"],
    )
    assert r.status_code == 409, r.text


def test_list_scoped_by_org(client, setup):
    _new_consult(client, setup)
    rows = client.get("/api/online-consults", headers=setup["doc"]).json()
    assert rows and all(c["org_id"] == setup["org"]["id"] for c in rows)
    # 他院医生查本院机构 → 403（scope_org_list 明确拒绝越权 org_id）
    assert client.get(
        f"/api/online-consults?org_id={setup['org']['id']}", headers=setup["doc2"]
    ).status_code == 403
