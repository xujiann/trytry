"""B2：会诊/转诊附件业务域——上传/列举/下载全链路与跨机构越权（对齐既有三类口径）。"""
import io

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app
from app.database import SessionLocal
from app.models import AccessLog

PNG = b"\x89PNG\r\n\x1a\n" + b"consult-attach" * 10


@pytest.fixture()
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture()
def world(client):
    """甲乙两家互不相干的县医院：甲院患者的会诊/转诊各一单。"""
    admin = login(client, "admin", "admin123")
    orgs, docs = {}, {}
    for tag in ("甲", "乙", "丙"):
        org = client.post(
            "/api/organizations",
            json={"name": f"附件{tag}院", "org_type": "lead_hospital", "level": "county"},
            headers=admin,
        ).json()
        client.post(
            "/api/users",
            json={"username": f"att_{tag}", "password": "pass123456", "role": "doctor", "org_id": org["id"]},
            headers=admin,
        )
        orgs[tag], docs[tag] = org, login(client, f"att_{tag}", "pass123456")
    client.post(
        "/api/users",
        json={"username": "att_ph", "password": "pass123456", "role": "public_health",
              "org_id": orgs["甲"]["id"]},
        headers=admin,
    )
    patient = client.post(
        "/api/patients",
        json={"name": "附件患者", "id_card": "330281199209096023"},
        headers=admin,
    ).json()
    consultation = client.post(
        "/api/consultations",
        json={"patient_id": patient["id"], "from_org_id": orgs["甲"]["id"],
              "to_org_id": orgs["乙"]["id"], "question": "肺部阴影影像会诊"},
        headers=docs["甲"],
    ).json()
    referral = client.post(
        "/api/referrals",
        json={"patient_id": patient["id"], "from_org_id": orgs["甲"]["id"],
              "to_org_id": orgs["乙"]["id"], "direction": "up", "reason": "转上级进一步诊治"},
        headers=docs["甲"],
    ).json()
    return {
        "admin": admin, "orgs": orgs, "docs": docs, "patient": patient,
        "ph": login(client, "att_ph", "pass123456"),
        "consultation": consultation, "referral": referral,
    }


def _upload(client, headers, owner_type, owner_id, filename="佐证.png"):
    return client.post(
        "/api/attachments",
        data={"owner_type": owner_type, "owner_id": str(owner_id)},
        files={"file": (filename, io.BytesIO(PNG), "image/png")},
        headers=headers,
    )


@pytest.mark.parametrize("owner_key", ["consultation", "referral"])
def test_上传列举下载全链路(client, world, owner_key):
    owner = world[owner_key]
    up = _upload(client, world["docs"]["甲"], owner_key, owner["id"])
    assert up.status_code == 201, up.text
    att = up.json()
    assert (att["owner_type"], att["owner_id"]) == (owner_key, owner["id"])
    listed = client.get(
        f"/api/attachments?owner_type={owner_key}&owner_id={owner['id']}",
        headers=world["docs"]["甲"],
    ).json()
    assert [a["id"] for a in listed] == [att["id"]]
    got = client.get(f"/api/attachments/{att['id']}", headers=world["docs"]["甲"])
    assert got.status_code == 200
    assert got.content == PNG
    # 患者档口径：下载写 AccessLog 留痕（resource=att:<owner>:download）
    with SessionLocal() as db:
        logs = db.query(AccessLog).filter(AccessLog.resource == f"att:{owner_key}:download").all()
        assert logs, "下载未留痕"
        assert all(log.patient_id == world["patient"]["id"] for log in logs)


@pytest.mark.parametrize("owner_key", ["consultation", "referral"])
def test_受邀转入机构可见_无关机构403(client, world, owner_key):
    """口径与患者档一致：会诊受邀方/转诊转入方与患者有服务关系（单子本身就是关系），
    可以取材料；与单子无关的丙院一律 403。"""
    owner = world[owner_key]
    up = _upload(client, world["docs"]["甲"], owner_key, owner["id"])
    att_id = up.json()["id"]
    # 乙院是受邀/转入方——看得到（这正是会诊/转诊要传材料的场景）
    assert client.get(f"/api/attachments/{att_id}", headers=world["docs"]["乙"]).status_code == 200
    outsider = world["docs"]["丙"]
    assert _upload(client, outsider, owner_key, owner["id"]).status_code == 403
    assert client.get(
        f"/api/attachments?owner_type={owner_key}&owner_id={owner['id']}", headers=outsider
    ).status_code == 403
    assert client.get(f"/api/attachments/{att_id}", headers=outsider).status_code == 403


def test_角色白名单_公卫不可上传(client, world):
    resp = _upload(client, world["ph"], "consultation", world["consultation"]["id"])
    assert resp.status_code == 403
    resp = _upload(client, world["ph"], "referral", world["referral"]["id"])
    assert resp.status_code == 403


def test_挂接对象不存在404(client, world):
    assert _upload(client, world["docs"]["甲"], "consultation", 99999).status_code == 404
    assert _upload(client, world["docs"]["甲"], "referral", 99999).status_code == 404
