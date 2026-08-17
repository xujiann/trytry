"""S3 绩效工资二次分配：建池、按公式二次分配、金额加总分毫等于总额、权限。

守的口径与 fund.py 结余分配同形：分配办法可换（公式）、账要分毫对得上（Hamilton）、
全零权重显式拒绝（不静默吞钱）、二次分配是管理层动作（director/admin）。
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
        json={"name": "绩效二次分配县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def director(client, admin, org):
    client.post(
        "/api/users",
        json={"username": "pd_dir", "password": "passw0rd1", "role": "director",
              "org_id": org["id"]},
        headers=admin,
    )
    resp = client.post("/api/auth/login", json={"username": "pd_dir", "password": "passw0rd1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def operator(client, admin, org):
    client.post(
        "/api/users",
        json={"username": "pd_op", "password": "passw0rd1", "role": "operator",
              "org_id": org["id"]},
        headers=admin,
    )
    resp = client.post("/api/auth/login", json={"username": "pd_op", "password": "passw0rd1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _pool(client, headers, year, source="mixed", total=200000.0):
    return client.post(
        "/api/perf-distribution/pools",
        json={"year": year, "source": source, "total_amount": total},
        headers=headers,
    )


# ---------------- 建池 ----------------


def test_建池并按年度查询(client, director):
    resp = _pool(client, director, "2030", total=100000.0)
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]
    listed = client.get("/api/perf-distribution/pools?year=2030", headers=director).json()
    assert any(p["id"] == pid for p in listed)


def test_同年度同范围同来源只能建一个池(client, director):
    _pool(client, director, "2031", source="fund_surplus")
    dup = _pool(client, director, "2031", source="fund_surplus", total=1.0)
    assert dup.status_code == 409


def test_admin_也可建池(client, admin):
    # admin 是全域角色，自动通过 director 守卫
    resp = _pool(client, admin, "2032", source="drg_surplus")
    assert resp.status_code == 201


# ---------------- 分配 ----------------


def _targets(scores):
    return [
        {"target_type": "org", "target_id": i + 1, "target_name": f"目标{i+1}", "score": s}
        for i, s in enumerate(scores)
    ]


def test_按绩效分配金额加总分毫等于总额(client, director):
    """三个目标按得分分配，分出去的总额必须**分毫等于**池内总额，不是约等于。"""
    pid = _pool(client, director, "2033", total=200000.0).json()["id"]
    resp = client.post(
        f"/api/perf-distribution/pools/{pid}/distribute",
        json={"formula_expr": "score", "targets": _targets([90, 60, 30])},
        headers=director,
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()["distributions"]
    assert len(rows) == 3
    assert round(sum(r["amount"] for r in rows), 2) == 200000.00
    assert abs(sum(r["share_pct"] for r in rows) - 100) < 0.01
    # 得分高的分得多——"钱跟着考核走"的最低要求
    ranked = sorted(rows, key=lambda r: -r["score"])
    assert ranked[0]["amount"] >= ranked[-1]["amount"]
    assert resp.json()["status"] == "distributed"


def test_均分公式除不尽仍分毫等于总额(client, director):
    """最大余数法（Hamilton）：均分 100.01 元 3 户，逐户各自 round 会丢分，
    合计对不上；这里钉住到分精确。"""
    pid = _pool(client, director, "2034", total=100.01).json()["id"]
    resp = client.post(
        f"/api/perf-distribution/pools/{pid}/distribute",
        json={"formula_expr": "1", "targets": _targets([0, 0, 0])},
        headers=director,
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()["distributions"]
    assert round(sum(r["amount"] for r in rows), 2) == 100.01


def test_重新分配覆盖而非叠加(client, director):
    pid = _pool(client, director, "2035", total=90000.0).json()["id"]
    for _ in range(3):
        client.post(
            f"/api/perf-distribution/pools/{pid}/distribute",
            json={"formula_expr": "1", "targets": _targets([10, 20, 30])},
            headers=director,
        )
    rows = client.get(
        f"/api/perf-distribution/pools/{pid}/distributions", headers=director
    ).json()
    assert len(rows) == 3, "重复分配叠加出了重复明细"


def test_全零权重显式拒绝不静默分零(client, director):
    """得分全为 0 时按 score 公式权重全零，无法归一化——必须 409，不能静默分 0。"""
    pid = _pool(client, director, "2036", total=50000.0).json()["id"]
    resp = client.post(
        f"/api/perf-distribution/pools/{pid}/distribute",
        json={"formula_expr": "score", "targets": _targets([0, 0, 0])},
        headers=director,
    )
    assert resp.status_code == 409
    # 拒绝后不得产生任何分配明细
    assert client.get(
        f"/api/perf-distribution/pools/{pid}/distributions", headers=director
    ).json() == []


def test_总额为零拒绝分配(client, director):
    pid = _pool(client, director, "2037", total=0.0).json()["id"]
    resp = client.post(
        f"/api/perf-distribution/pools/{pid}/distribute",
        json={"formula_expr": "1", "targets": _targets([1, 2])},
        headers=director,
    )
    assert resp.status_code == 409


def test_非法公式被拒(client, director):
    pid = _pool(client, director, "2038", total=1000.0).json()["id"]
    for expr in ("__import__('os')", "score + unknown_var", "score +"):
        resp = client.post(
            f"/api/perf-distribution/pools/{pid}/distribute",
            json={"formula_expr": expr, "targets": _targets([1, 2])},
            headers=director,
        )
        assert resp.status_code == 422, expr


# ---------------- 权限 ----------------


def test_二次分配限管理层(client, operator):
    resp = _pool(client, operator, "2039")
    assert resp.status_code == 403


def test_经办不可执行分配(client, admin, operator):
    pid = _pool(client, admin, "2040", total=1000.0).json()["id"]
    resp = client.post(
        f"/api/perf-distribution/pools/{pid}/distribute",
        json={"formula_expr": "1", "targets": _targets([1, 2])},
        headers=operator,
    )
    assert resp.status_code == 403
