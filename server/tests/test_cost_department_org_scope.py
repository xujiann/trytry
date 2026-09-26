"""科室成本汇总不带机构号时收进调用方的统计可见范围（P1-172）。

`department_cost_summary` 的守卫只有 `assert_org_visible(db, user, org_id)`——它对不带机构号直接放行，机构筛选
又只在 `if org_id is not None` 里：与试算平衡表（P1-155）同一形状。原先任一登录账号调
`/api/cost/departments?period=…` 不带机构号，就拿到全县各院的科室直接成本、成本构成与分摊。
口径照 P1-155：全域角色看全县；不带范围收到本机构 + 同医共体成员；带了机构号的路径不动。

另钉一道零基线闸门：读接口（`@router.get`）收可选的机构参数、守卫只有 `assert_org_visible`、却不调任何收范围的
助手——这一形状读侧棘轮（把 `assert_org_visible` 当授权）与清单探针（带必填参数的 GET 一律跳过）都数不到。
"""
import ast
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
#: 不带机构号时把范围收进可见机构的助手
SCOPERS = {"scope_stats_orgs", "scope_org_list", "visible_org_ids", "stats_org_ids"}


def _login(client, username, password="pw123456"):
    token = client.post("/api/auth/login", json={"username": username, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    """甲县医院与丙卫生院同属一个分组；乙卫生院谁都不挨着。甲院内科 8 月有一笔人员经费。"""
    orgs = {}
    for key, name, otype, level in (("a", "P1172 甲县医院", "lead_hospital", "county"),
                                    ("b", "P1172 乙卫生院", "township", "township"),
                                    ("c", "P1172 丙卫生院", "township", "township")):
        orgs[key] = client.post("/api/organizations", headers=admin,
                                json={"name": name, "org_type": otype, "level": level}).json()["id"]
    group = client.post("/api/org-groups", headers=admin, json={"name": "P1172 片区", "lead_org_id": orgs["a"]}).json()
    for key in ("a", "c"):
        assert client.post(f"/api/org-groups/{group['id']}/members", headers=admin,
                           json={"org_id": orgs[key]}).status_code == 201
    for key in ("b", "c"):
        assert client.post("/api/users", headers=admin, json={
            "username": f"p1172_doc_{key}", "password": "pw123456", "role": "doctor", "org_id": orgs[key]}).status_code == 201
    dept = client.post("/api/mgmt/departments", headers=admin, json={
        "org_id": orgs["a"], "code": "P1172NK", "name": "P1172 内科", "category": "clinical"})
    assert dept.status_code == 201, dept.text
    cost = client.post("/api/cost/departments", headers=admin, json={
        "dept_id": dept.json()["id"], "period": "2026-08", "cost_type": "labor", "amount": 86400})
    assert cost.status_code == 201, cost.text
    return {"b": _login(client, "p1172_doc_b"), "c": _login(client, "p1172_doc_c"), "dept": dept.json()["id"]}


def _labor(client, headers, dept_id):
    body = client.get("/api/cost/departments?period=2026-08", headers=headers)
    assert body.status_code == 200, body.text
    return sum(row["by_type"]["labor"] for row in body.json() if row["dept_id"] == dept_id)


def test_与谁都不挨着的卫生院看不到县医院的科室成本(client, world):
    assert _labor(client, world["b"], world["dept"]) == 0          # 修前 86400


def test_同分组成员与全域角色照旧看得到(client, admin, world):
    assert _labor(client, world["c"], world["dept"]) == 86400
    assert _labor(client, admin, world["dept"]) == 86400


# ================================================================ 零基线闸门
def _optional_org_reads_guarded_only_by_assert() -> list[str]:
    """`@router.get` 处理函数：有默认 None 的机构参数、调 `assert_org_visible`、不调任何 SCOPERS。"""
    hits = []
    for path in sorted(APP.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "get"
                       for d in fn.decorator_list):
                continue
            positional = fn.args.args[len(fn.args.args) - len(fn.args.defaults):]
            pairs = list(zip(positional, fn.args.defaults)) + list(zip(fn.args.kwonlyargs, fn.args.kw_defaults))
            if not any("org" in a.arg and isinstance(d, ast.Constant) and d.value is None for a, d in pairs):
                continue
            called = {n.func.id if isinstance(n.func, ast.Name) else n.func.attr
                      for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))}
            if "assert_org_visible" in called and not called & SCOPERS:
                hits.append(f"{path.relative_to(APP).as_posix()}::{fn.name}")
    return hits


def test_可选机构参数的读接口不能只靠assert_org_visible():
    hits = _optional_org_reads_guarded_only_by_assert()
    assert hits == [], (
        "`assert_org_visible` 对不带机构号直接放行——不带机构号的分支要收进可见范围"
        "（照 P1-155 / P1-172 用 `scope_stats_orgs(db, user, None)`，或 `scope_org_list`）：\n  " + "\n  ".join(hits)
    )


def test_判据自证(tmp_path, monkeypatch):
    (tmp_path / "probe.py").write_text(
        "@router.get('/a')\n"
        "def leaky(period: str, org_id: int | None = None, db=None, user=None):\n"   # 该命中
        "    assert_org_visible(db, user, org_id)\n"
        "@router.get('/b')\n"
        "def scoped(org_id: int | None = None, db=None, user=None):\n"               # 收了范围，不算
        "    assert_org_visible(db, user, org_id)\n"
        "    scope = scope_stats_orgs(db, user, None)\n"
        "@router.get('/c')\n"
        "def required(org_id: int, db=None, user=None):\n"                          # 机构号必填，不算
        "    assert_org_visible(db, user, org_id)\n"
        "@router.post('/d')\n"
        "def write(org_id: int | None = None, db=None, user=None):\n"               # 不是读接口，不算
        "    assert_org_visible(db, user, org_id)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "APP", tmp_path)
    assert _optional_org_reads_guarded_only_by_assert() == ["probe.py::leaky"]
