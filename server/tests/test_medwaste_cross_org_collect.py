"""医废收集登记必须校验机构归属——body 带 org_id 的横向越权，矩阵看不见。

`POST /api/medwaste` 此前只查机构**存在**，不查经办人能不能往这家机构写，
而同模块的 `create_location` / `store` / `handover` 三个都查了。于是乙院经办
可以拿甲院的 org_id 登记医废，记到别家账上——医废台账是要对监管报数的。

**为什么两道既有闸门都没抓到它**：

* `tests/test_stage15_horizontal.py` 的判据是「入参含患者标识」或「按 id 直取
  患者资源」，而这条的机构标识走 body，且医废不是患者维度数据；
* `tests/test_cross_org_write_guards.py` 的判据是 `/{id}` 型直取。

判据看不见的形状不等于安全。这一条按业务写死回归，缺口本身登记为 P1-39。
"""
import pytest
from conftest import reset_database
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def two_orgs(client, admin):
    a = client.post("/api/organizations", json={
        "name": "医废甲院", "org_type": "lead_hospital", "level": "county"},
        headers=admin).json()
    b = client.post("/api/organizations", json={
        "name": "医废乙卫生院", "org_type": "township", "level": "township"},
        headers=admin).json()
    return a, b


@pytest.fixture(scope="module")
def operator_of_b(client, admin, two_orgs):
    _a, b = two_orgs
    client.post("/api/users", json={
        "username": "mw_op_b", "password": "Mwop#2026x", "name": "乙院经办",
        "role": "operator", "org_id": b["id"]}, headers=admin)
    token = client.post("/api/auth/login", json={
        "username": "mw_op_b", "password": "Mwop#2026x"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _body(org_id: int) -> dict:
    return {"org_id": org_id, "waste_type": "sharp", "weight_kg": 0.5,
            "collected_date": "2026-09-19"}


def test_跨机构登记医废被拒(client, two_orgs, operator_of_b):
    """去掉 collect 里的 assert_org_writable，本条必红——乙院经办把医废记到甲院头上。"""
    a, _b = two_orgs
    resp = client.post("/api/medwaste", json=_body(a["id"]), headers=operator_of_b)
    assert resp.status_code == 403, (
        f"乙院经办把医废登记到甲院成功了（{resp.status_code}）：{resp.text}"
    )


def test_登记本机构医废照常(client, two_orgs, operator_of_b):
    """守卫不能误伤本职工作：同一个经办往自己机构登记必须照常成功。"""
    _a, b = two_orgs
    resp = client.post("/api/medwaste", json=_body(b["id"]), headers=operator_of_b)
    assert resp.status_code == 201, resp.text
    assert resp.json()["org_id"] == b["id"]
    assert resp.json()["trace_code"], "追溯码没生成"


def test_全域角色不受限(client, admin, two_orgs):
    """admin/director 是全域角色，跨机构登记是它们的正常职权，不该被这道守卫挡住。"""
    a, _b = two_orgs
    resp = client.post("/api/medwaste", json=_body(a["id"]), headers=admin)
    assert resp.status_code == 201, resp.text
