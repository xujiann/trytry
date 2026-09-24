"""调阅授权的「发放」「撤销」留痕（P1-72）。

授权管理按设计可问责而非可阻断（`visibility.log_patient_access` 的 docstring 点名的就是
`patients.authorizations`）：患者本人在柜台前，本机构此刻往往还没有他的任何记录。同文件的
授权清单与校验两个读接口早就按这个口径留痕（`basis="consent_admin"`），可偏偏**改变
"谁能调阅这个人"的两个写动作**——发授权、撤授权——一条 AccessLog 都不写：居民在
「谁看过我的档案」里看不到是谁替他办的，而发出去的授权立刻就是别家调阅他档案的依据。

补上：两处照同文件口径只留痕、不阻断。两个方向都钉：与患者无关的机构照样办得了
（按设计），但办了必留一条带办理人与「授权办理」依据的痕。
"""
import pytest

from app.database import SessionLocal
from app.models import AccessLog, ArchiveAuthorization


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def auth_world(client):
    """患者与乙院毫无关系；乙院窗口经办替他办授权（患者本人在柜台前）。"""
    admin = _login(client, "admin", "admin123")
    b = client.post("/api/organizations",
                    json={"name": "授权留痕乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    grantee = client.post("/api/organizations",
                          json={"name": "授权留痕丙院", "org_type": "township", "level": "township"},
                          headers=admin).json()
    r = client.post("/api/users",
                    json={"username": "p172_op_b", "password": "pw123456", "full_name": "乙院窗口",
                          "role": "operator", "org_id": b["id"]},
                    headers=admin)
    assert r.status_code == 201, r.text
    patient = client.post("/api/patients",
                          json={"name": "授权留痕患者", "id_card": "320000198912126675"},
                          headers=admin).json()
    return {"op_b": _login(client, "p172_op_b"), "patient_id": patient["id"], "grantee": grantee["id"]}


def _trace(patient_id: int) -> list[tuple[str, str, str]]:
    db = SessionLocal()
    try:
        rows = db.query(AccessLog).filter(
            AccessLog.patient_id == patient_id, AccessLog.resource == "authorization"
        ).order_by(AccessLog.id).all()
        return [(r.username, r.resource, r.basis) for r in rows]
    finally:
        db.close()


def test_窗口发授权按设计不阻断_但必留痕(client, auth_world):
    pid = auth_world["patient_id"]
    before = _trace(pid)
    r = client.post(f"/api/patients/{pid}/authorizations",
                    json={"grantee_org_id": auth_world["grantee"], "scope": "all", "expire_date": "2027-12-31"},
                    headers=auth_world["op_b"])
    assert r.status_code == 201, r.text
    assert _trace(pid) == before + [("p172_op_b", "authorization", "consent_admin")]


def test_窗口撤授权按设计不阻断_但必留痕(client, auth_world):
    pid = auth_world["patient_id"]
    db = SessionLocal()
    try:
        auth = ArchiveAuthorization(patient_id=pid, grantee_org_id=auth_world["grantee"],
                                    expire_date="2027-12-31", created_by=1)
        db.add(auth)
        db.commit()
        auth_id = auth.id
    finally:
        db.close()
    before = _trace(pid)
    r = client.post(f"/api/patients/{pid}/authorizations/{auth_id}/revoke", headers=auth_world["op_b"])
    assert r.status_code == 200, r.text
    assert _trace(pid) == before + [("p172_op_b", "authorization", "consent_admin")]


def test_授权号不存在照旧404且不留痕(client, auth_world):
    pid = auth_world["patient_id"]
    before = _trace(pid)
    r = client.post(f"/api/patients/{pid}/authorizations/987654/revoke", headers=auth_world["op_b"])
    assert r.status_code == 404, r.text
    assert _trace(pid) == before
