"""S14 EMPI 跨机构主索引合并（merge/link）用例。

守四条：合并后 resolve 能跟到规范主档；主档 resolve 到自身；同一重复档不能
被重复合并（409）；自合并被拒（422）；撤销后可重新合并（design (b)：revert 删行
释放 duplicate）；主索引合并是平台级动作，非管理员写不了（403）。
"""
import itertools

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

_id_seq = itertools.count(1)


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
def operator(client, admin):
    client.post(
        "/api/users",
        json={"username": "empi_op", "password": "passw0rd1", "role": "operator"},
        headers=admin,
    )
    resp = client.post("/api/auth/login", json={"username": "empi_op", "password": "passw0rd1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _new_patient(client, admin, name="主索引患者"):
    """每次造一条全新患者（唯一身份证号），避免跨用例的唯一约束串味。"""
    n = next(_id_seq)
    id_card = f"3317821990{n:08d}"  # 18 位
    resp = client.post(
        "/api/patients", json={"name": name, "id_card": id_card}, headers=admin
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _merge(client, headers, primary, duplicate, reason="录错身份证"):
    return client.post(
        "/api/empi/merge",
        json={
            "primary_patient_id": primary["id"],
            "duplicate_patient_id": duplicate["id"],
            "reason": reason,
        },
        headers=headers,
    )


# ---------------------------------------------------------------- 合并与解析
def test_合并两条主档并可解析到规范档(client, admin):
    primary = _new_patient(client, admin, "李规范")
    dup = _new_patient(client, admin, "李重复")
    resp = _merge(client, admin, primary, dup)
    assert resp.status_code == 201, resp.text
    link = resp.json()
    assert link["status"] == "merged"
    assert link["primary_patient_id"] == primary["id"]
    assert link["duplicate_patient_id"] == dup["id"]

    # resolve(重复档) → 规范主档
    r = client.get(f"/api/empi/resolve/{dup['id']}", headers=admin).json()
    assert r["input_id"] == dup["id"]
    assert r["canonical_id"] == primary["id"]
    assert r["canonical"]["id"] == primary["id"]
    assert r["canonical"]["ehc_no"] == primary["ehc_no"]

    # resolve(主档) → 自身
    r2 = client.get(f"/api/empi/resolve/{primary['id']}", headers=admin).json()
    assert r2["canonical_id"] == primary["id"]


def test_列出患者相关的合并(client, admin):
    primary = _new_patient(client, admin)
    dup = _new_patient(client, admin)
    _merge(client, admin, primary, dup)
    rows = client.get(f"/api/empi/links?patient_id={dup['id']}", headers=admin).json()
    assert len(rows) == 1
    assert rows[0]["duplicate_patient_id"] == dup["id"]
    # 主档视角也能查到
    rows_p = client.get(f"/api/empi/links?patient_id={primary['id']}", headers=admin).json()
    assert any(r["primary_patient_id"] == primary["id"] for r in rows_p)


def test_重复档不能被重复合并(client, admin):
    primary = _new_patient(client, admin)
    other = _new_patient(client, admin)
    dup = _new_patient(client, admin)
    assert _merge(client, admin, primary, dup).status_code == 201
    # 同一重复档再并到另一个主档 → 唯一约束 409
    resp = _merge(client, admin, other, dup)
    assert resp.status_code == 409


def test_自合并被拒(client, admin):
    p = _new_patient(client, admin)
    resp = _merge(client, admin, p, p)
    assert resp.status_code == 422


def test_主档本身已被合并则拒绝(client, admin):
    a = _new_patient(client, admin)
    b = _new_patient(client, admin)
    c = _new_patient(client, admin)
    assert _merge(client, admin, a, b).status_code == 201  # b 是 a 的重复档
    # 再想把 c 并到 b（b 本身已是重复档）→ 422
    resp = _merge(client, admin, b, c)
    assert resp.status_code == 422
    assert "主档本身已被合并" in resp.json()["detail"]


def test_不存在的患者报404(client, admin):
    p = _new_patient(client, admin)
    r1 = client.post(
        "/api/empi/merge",
        json={"primary_patient_id": 999999, "duplicate_patient_id": p["id"]},
        headers=admin,
    )
    assert r1.status_code == 404
    r2 = client.post(
        "/api/empi/merge",
        json={"primary_patient_id": p["id"], "duplicate_patient_id": 999999},
        headers=admin,
    )
    assert r2.status_code == 404


def test_撤销后可重新合并(client, admin):
    primary = _new_patient(client, admin)
    dup = _new_patient(client, admin)
    link = _merge(client, admin, primary, dup).json()

    # 撤销：删行释放 duplicate
    rev = client.post(f"/api/empi/links/{link['id']}/revert", headers=admin)
    assert rev.status_code == 200
    assert rev.json()["reverted"] is True

    # 撤销后 resolve(重复档) → 回到自身
    r = client.get(f"/api/empi/resolve/{dup['id']}", headers=admin).json()
    assert r["canonical_id"] == dup["id"]

    # 二次撤销：行已不存在 → 404
    assert client.post(
        f"/api/empi/links/{link['id']}/revert", headers=admin
    ).status_code == 404

    # 重新合并同一重复档到新主档 → 成功（design (b) 的关键收益）
    new_primary = _new_patient(client, admin)
    again = _merge(client, admin, new_primary, dup)
    assert again.status_code == 201


def test_非管理员不能合并(client, admin, operator):
    primary = _new_patient(client, admin)
    dup = _new_patient(client, admin)
    resp = _merge(client, operator, primary, dup)
    assert resp.status_code == 403


def test_resolve_requires_patient_visibility(client, admin, operator):
    """F1 回归：resolve 返回姓名/电子健康卡号须过患者可见性，无关系的非全域账号 403。"""
    p = _new_patient(client, admin)
    assert client.get(f"/api/empi/resolve/{p['id']}", headers=admin).status_code == 200
    assert client.get(f"/api/empi/resolve/{p['id']}", headers=operator).status_code == 403


def test_merge_chain_rejected(client, admin):
    """MED-3 回归：已是主档的档不能再作为重复档并入（避免 A→B→C 两跳断链）。"""
    a = _new_patient(client, admin)
    b = _new_patient(client, admin)
    cc = _new_patient(client, admin)
    assert client.post("/api/empi/merge", json={"primary_patient_id": b["id"], "duplicate_patient_id": a["id"]}, headers=admin).status_code == 201
    assert client.post("/api/empi/merge", json={"primary_patient_id": cc["id"], "duplicate_patient_id": b["id"]}, headers=admin).status_code == 422
