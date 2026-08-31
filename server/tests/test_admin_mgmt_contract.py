"""综合管理 `/api/mgmt` 二十个待治理端点的**特征化网 + 响应契约**。

套路同 test_quality_contract.py / test_education_contract.py：先补网钉住**当前**
响应的完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。（employees/finance 建账/assets/docs/rosters/qc 等 14 个端点
此前已治理，不在本网范围，仅作种子。）

本簇的建模判断（都以此处的精确断言为依据）：

- Money 列（`finance_entries.amount` / `payroll_records.base_salary`、`perf_bonus`、
  `total` / `budgets.amount`）：整数金额读回来是 **int**（5000/6000/10000），带小数
  才是 float——包括 `SUM()`/`round()` 派生值（`finance/summary` 的 income、
  `payroll` 的 total_amount、`budgets/execution` 的 actual）。声明 `int | float`
  原样透传；声明 `float` 会把 `6000` 变 `6000.0`（陷阱一，此处以 type 断言钉死）。
- `perf_coefficient` 是 **Float** 列：整数系数读回来就是 `1.0`，声明 `float` 才是
  原样——与同一行里的 Money 列相反，判据是列类型不是字段名。
- `finance/summary` 未记账一侧保持初始字面量 `0.0`（float），而全空分支的
  consolidated 是 `round(sum([]), 2)` = **0（int）**——同一个字段两种取值，
  `int | float` 是唯一不改字节的声明。
- `budgets/execution` 的 `execution_pct` 是**值可空**而非条件键：键恒在，无预算时
  值为 null——声明 `float | None` 即可，无需 exclude_unset。本簇没有条件键。
- `staff-contracts` 新建回执（3 键）、到期提醒行（4 键）、列表行（6 键）三种形状
  ——三个模型，不许互相注入。
- `AssetMovement.created_at.isoformat()` / `SystemParam.updated_at.isoformat()`
  出参是 ISO 字符串，声明 `str`（值随运行时间变，此处只钉键与类型）。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

SECONDMENT_RECEIPT_KEYS = ["id", "employee_id", "status"]
FINANCE_ORG_ROW_KEYS = ["org_id", "income", "expense", "balance"]
PAYROLL_ROW_KEYS = [
    "id", "employee_id", "period", "base_salary", "perf_bonus", "perf_coefficient", "total",
]
CONTRACT_ROW_KEYS = ["id", "employee_id", "contract_no", "start_date", "end_date", "status"]
MOVEMENT_ROW_KEYS = ["id", "movement_type", "quantity", "note", "at"]
PARAM_ROW_KEYS = ["key", "value", "description", "updated_at"]


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def base(client, admin):
    """两家机构 + 各一名员工（经已治理的建档端点种入）。"""
    org1 = client.post(
        "/api/organizations",
        json={"name": "综管契约医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    org2 = client.post(
        "/api/organizations",
        json={"name": "综管契约卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    emp1 = client.post(
        "/api/mgmt/employees",
        json={"org_id": org1["id"], "name": "郭员工", "title": "副主任医师", "position": "内科医师"},
        headers=admin,
    ).json()
    emp2 = client.post(
        "/api/mgmt/employees", json={"org_id": org2["id"], "name": "何员工"}, headers=admin
    ).json()
    return {"org1": org1, "org2": org2, "emp1": emp1, "emp2": emp2}


# ---------------------------------------------------------------- ㉚ 人员派驻


@pytest.fixture(scope="module")
def secondments(client, admin, base):
    """emp1 派驻→结束（回到在岗），emp2 派驻在途——stats 两个口径都非零。"""
    s1 = client.post(
        "/api/mgmt/secondments",
        json={"employee_id": base["emp1"]["id"], "to_org_id": base["org2"]["id"], "start_date": "2026-08-01"},
        headers=admin,
    )
    assert s1.status_code == 201, s1.text
    ended = client.post(f"/api/mgmt/secondments/{s1.json()['id']}/end?end_date=2026-08-20", headers=admin)
    assert ended.status_code == 200, ended.text
    s2 = client.post(
        "/api/mgmt/secondments",
        json={"employee_id": base["emp2"]["id"], "to_org_id": base["org1"]["id"], "start_date": "2026-08-15"},
        headers=admin,
    )
    assert s2.status_code == 201, s2.text
    return {"s1": s1.json(), "ended": ended.json(), "s2": s2.json()}


def test_派驻回执与结束回执精确(base, secondments):
    body = secondments["s1"]
    assert list(body.keys()) == SECONDMENT_RECEIPT_KEYS
    assert body == {"id": body["id"], "employee_id": base["emp1"]["id"], "status": "seconded"}
    assert list(secondments["ended"].keys()) == ["id", "end_date"]
    assert secondments["ended"] == {"id": body["id"], "end_date": "2026-08-20"}
    assert secondments["s2"] == {
        "id": secondments["s2"]["id"], "employee_id": base["emp2"]["id"], "status": "seconded"
    }


def test_派驻统计精确(client, admin, secondments):
    resp = client.get("/api/mgmt/secondments/stats", headers=admin)
    assert list(resp.json().keys()) == ["active_secondments", "total_secondments"]
    assert resp.json() == {"active_secondments": 1, "total_secondments": 2}


# ---------------------------------------------------------------- ㉛ 集中核算


@pytest.fixture(scope="module")
def finance(client, admin, base):
    """org1 整数收入 + 小数支出、org2 只有收入——int/float 与 0.0 兜底同时在场。"""
    for payload in [
        {"org_id": base["org1"]["id"], "period": "2026-07", "category": "income", "item": "医疗收入", "amount": 2000},
        {"org_id": base["org1"]["id"], "period": "2026-07", "category": "expense", "item": "药品支出", "amount": 500.5},
        {"org_id": base["org2"]["id"], "period": "2026-08", "category": "income", "amount": 300},
    ]:
        resp = client.post("/api/mgmt/finance", json=payload, headers=admin)
        assert resp.status_code == 201, resp.text
    return True


def test_集中核算汇总精确_Money整数是int(client, admin, base, finance):
    body = client.get("/api/mgmt/finance/summary", headers=admin).json()
    assert list(body.keys()) == ["period", "orgs", "consolidated"]
    assert [list(o.keys()) for o in body["orgs"]] == [FINANCE_ORG_ROW_KEYS] * 2
    assert list(body["consolidated"].keys()) == ["income", "expense", "balance"]
    assert body == {
        "period": "全部",
        "orgs": [
            {"org_id": base["org1"]["id"], "income": 2000, "expense": 500.5, "balance": 1499.5},
            {"org_id": base["org2"]["id"], "income": 300, "expense": 0.0, "balance": 300.0},
        ],
        "consolidated": {"income": 2300, "expense": 500.5, "balance": 1799.5},
    }
    # Money 列 SUM 后的整数是 int；未记账一侧保持初始字面量 0.0（float）——
    # 声明 float 会把 2000 变 2000.0，声明 int 会 500.5 验证失败
    org1_row, org2_row = body["orgs"]
    assert type(org1_row["income"]) is int and type(body["consolidated"]["income"]) is int
    assert isinstance(org1_row["expense"], float) and isinstance(org2_row["expense"], float)
    assert isinstance(org2_row["balance"], float)


def test_集中核算_期间过滤与空分支精确(client, admin, base, finance):
    filtered = client.get("/api/mgmt/finance/summary?period=2026-07", headers=admin).json()
    assert filtered == {
        "period": "2026-07",
        "orgs": [{"org_id": base["org1"]["id"], "income": 2000, "expense": 500.5, "balance": 1499.5}],
        "consolidated": {"income": 2000, "expense": 500.5, "balance": 1499.5},
    }
    # 全空分支：round(sum([]), 2) 是 int 0，不是 0.0
    empty = client.get("/api/mgmt/finance/summary?period=1999-01", headers=admin).json()
    assert empty == {"period": "1999-01", "orgs": [], "consolidated": {"income": 0, "expense": 0, "balance": 0}}
    assert all(type(v) is int for v in empty["consolidated"].values())


# ---------------------------------------------------------------- 科室基础库


@pytest.fixture(scope="module")
def departments(client, admin, base):
    d1 = client.post(
        "/api/mgmt/departments",
        json={"org_id": base["org1"]["id"], "code": "NK", "name": "内科"},
        headers=admin,
    )
    assert d1.status_code == 201, d1.text
    d2 = client.post(
        "/api/mgmt/departments",
        json={"org_id": base["org1"]["id"], "code": "YJ", "name": "医技科", "category": "medtech"},
        headers=admin,
    ).json()
    assigned = client.post(
        f"/api/mgmt/employees/{base['emp1']['id']}/department?dept_id={d1.json()['id']}",
        headers=admin,
    ).json()
    return {"d1": d1.json(), "d2": d2, "assigned": assigned}


def test_科室建档回执与清单精确(client, admin, base, departments):
    body = departments["d1"]
    assert list(body.keys()) == ["id", "org_id", "code", "name"]
    assert body == {"id": body["id"], "org_id": base["org1"]["id"], "code": "NK", "name": "内科"}
    rows = client.get("/api/mgmt/departments", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [["id", "org_id", "code", "name", "category"]] * 2
    assert rows == [
        {**departments["d1"], "category": "clinical"},
        {**departments["d2"], "category": "medtech"},
    ]  # 按 org_id、code 排序
    assert client.get(
        f"/api/mgmt/departments?org_id={base['org2']['id']}", headers=admin
    ).json() == []


def test_员工科室挂接回执精确(base, departments):
    assert list(departments["assigned"].keys()) == ["employee_id", "dept_id"]
    assert departments["assigned"] == {
        "employee_id": base["emp1"]["id"],
        "dept_id": departments["d1"]["id"],
    }


# ---------------------------------------------------------------- 人员变动


@pytest.fixture(scope="module")
def changes(client, admin, base, departments):
    """依赖 departments：科室挂接须发生在跨机构调动清空 dept_id 之前。"""
    hired = client.post(
        f"/api/mgmt/employees/{base['emp1']['id']}/changes",
        json={"change_type": "hire", "detail": "新员工入职", "effective_date": "2026-01-01"},
        headers=admin,
    )
    assert hired.status_code == 201, hired.text
    transferred = client.post(
        f"/api/mgmt/employees/{base['emp1']['id']}/changes",
        json={"change_type": "transfer", "to_org_id": base["org2"]["id"]},
        headers=admin,
    ).json()
    return {"hired": hired.json(), "transferred": transferred}


def test_人员变动回执精确_两种变动(base, changes):
    body = changes["hired"]
    assert list(body.keys()) == ["id", "employee_id", "change_type", "employee_status", "employee_org_id"]
    assert body == {
        "id": body["id"],
        "employee_id": base["emp1"]["id"],
        "change_type": "hire",
        "employee_status": "active",
        "employee_org_id": base["org1"]["id"],
    }
    # 调动：员工机构联动变更
    assert changes["transferred"] == {
        "id": changes["transferred"]["id"],
        "employee_id": base["emp1"]["id"],
        "change_type": "transfer",
        "employee_status": "active",
        "employee_org_id": base["org2"]["id"],
    }


def test_人员变动清单精确_to_org_id两种取值(client, admin, base, changes):
    rows = client.get(f"/api/mgmt/employees/{base['emp1']['id']}/changes", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [["id", "change_type", "to_org_id", "detail", "effective_date"]] * 2
    assert rows == [
        {"id": changes["hired"]["id"], "change_type": "hire", "to_org_id": None,
         "detail": "新员工入职", "effective_date": "2026-01-01"},
        {"id": changes["transferred"]["id"], "change_type": "transfer",
         "to_org_id": base["org2"]["id"], "detail": "", "effective_date": ""},
    ]


# ---------------------------------------------------------------- 合同管理


@pytest.fixture(scope="module")
def contracts(client, admin, base):
    c1 = client.post(
        "/api/mgmt/staff-contracts",
        json={"employee_id": base["emp1"]["id"], "contract_no": "MGT-HT-001",
              "start_date": "2026-01-01", "end_date": "2026-10-15"},
        headers=admin,
    )
    assert c1.status_code == 201, c1.text
    c2 = client.post(
        "/api/mgmt/staff-contracts",
        json={"employee_id": base["emp2"]["id"], "contract_no": "MGT-HT-002",
              "start_date": "2026-01-01", "end_date": "2027-12-31"},
        headers=admin,
    ).json()
    return {"c1": c1.json(), "c2": c2}


def test_合同回执与到期提醒精确(client, admin, base, contracts):
    body = contracts["c1"]
    assert list(body.keys()) == ["id", "contract_no", "status"]
    assert body == {"id": body["id"], "contract_no": "MGT-HT-001", "status": "active"}
    # 到期提醒行是 4 键形状（与 6 键列表行不同形），today 固定注入保证可复算
    rows = client.get("/api/mgmt/staff-contracts/expiring?today=2026-09-01", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [["id", "employee_id", "contract_no", "end_date"]]
    assert rows == [{
        "id": contracts["c1"]["id"],
        "employee_id": base["emp1"]["id"],
        "contract_no": "MGT-HT-001",
        "end_date": "2026-10-15",
    }]


def test_合同清单精确(client, admin, base, contracts):
    rows = client.get("/api/mgmt/staff-contracts", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [CONTRACT_ROW_KEYS] * 2
    c1_row = {"id": contracts["c1"]["id"], "employee_id": base["emp1"]["id"],
              "contract_no": "MGT-HT-001", "start_date": "2026-01-01",
              "end_date": "2026-10-15", "status": "active"}
    c2_row = {"id": contracts["c2"]["id"], "employee_id": base["emp2"]["id"],
              "contract_no": "MGT-HT-002", "start_date": "2026-01-01",
              "end_date": "2027-12-31", "status": "active"}
    assert rows == [c2_row, c1_row]  # id 倒序
    assert client.get(
        f"/api/mgmt/staff-contracts?employee_id={base['emp1']['id']}", headers=admin
    ).json() == [c1_row]


# ---------------------------------------------------------------- 薪酬


@pytest.fixture(scope="module")
def payroll(client, admin, base):
    p1 = client.post(
        "/api/mgmt/payroll",
        json={"employee_id": base["emp1"]["id"], "period": "2026-07",
              "base_salary": 5000, "perf_bonus": 1000, "perf_coefficient": 1.0},
        headers=admin,
    )
    assert p1.status_code == 201, p1.text
    p2 = client.post(
        "/api/mgmt/payroll",
        json={"employee_id": base["emp2"]["id"], "period": "2026-07",
              "base_salary": 4000.25, "perf_bonus": 500.5, "perf_coefficient": 0.8},
        headers=admin,
    ).json()
    return {"p1": p1.json(), "p2": p2}


def test_薪酬回执精确_Money整数是int(base, payroll):
    body = payroll["p1"]
    assert list(body.keys()) == ["id", "period", "total"]
    # 回执经 db.refresh 读回 Money 列：整数薪酬是 int 6000，不是 6000.0
    assert body == {"id": body["id"], "period": "2026-07", "total": 6000}
    assert type(body["total"]) is int
    assert payroll["p2"] == {"id": payroll["p2"]["id"], "period": "2026-07", "total": 4400.65}
    assert isinstance(payroll["p2"]["total"], float)


def test_薪酬清单精确_Float系数与Money并存(client, admin, base, payroll):
    body = client.get("/api/mgmt/payroll", headers=admin).json()
    assert list(body.keys()) == ["total_amount", "records"]
    assert [list(r.keys()) for r in body["records"]] == [PAYROLL_ROW_KEYS] * 2
    p1_row = {"id": payroll["p1"]["id"], "employee_id": base["emp1"]["id"], "period": "2026-07",
              "base_salary": 5000, "perf_bonus": 1000, "perf_coefficient": 1.0, "total": 6000}
    p2_row = {"id": payroll["p2"]["id"], "employee_id": base["emp2"]["id"], "period": "2026-07",
              "base_salary": 4000.25, "perf_bonus": 500.5, "perf_coefficient": 0.8, "total": 4400.65}
    assert body == {"total_amount": 10400.65, "records": [p2_row, p1_row]}  # id 倒序
    got_p1 = body["records"][1]
    # Money 列整数原样 int；perf_coefficient 是 Float 列，整数系数读回来就是 1.0
    assert type(got_p1["base_salary"]) is int and type(got_p1["perf_bonus"]) is int
    assert type(got_p1["total"]) is int
    assert isinstance(got_p1["perf_coefficient"], float)
    # 过滤分支 + 空分支：round(sum([]), 2) 是 int 0
    only_p1 = client.get(
        f"/api/mgmt/payroll?period=2026-07&employee_id={base['emp1']['id']}", headers=admin
    ).json()
    assert only_p1 == {"total_amount": 6000, "records": [p1_row]}
    empty = client.get("/api/mgmt/payroll?period=1999-01", headers=admin).json()
    assert empty == {"total_amount": 0, "records": []}
    assert type(empty["total_amount"]) is int


# ---------------------------------------------------------------- 预算


@pytest.fixture(scope="module")
def budgets(client, admin, base, finance):
    """依赖 finance：执行数取自集中核算的 2026-07 记账。"""
    b1 = client.post(
        "/api/mgmt/budgets",
        json={"org_id": base["org1"]["id"], "year": "2026", "category": "income", "amount": 10000},
        headers=admin,
    )
    assert b1.status_code == 201, b1.text
    adjusted = client.post(
        "/api/mgmt/budgets",
        json={"org_id": base["org1"]["id"], "year": "2026", "category": "income", "amount": 8000.5},
        headers=admin,
    ).json()
    return {"b1": b1.json(), "adjusted": adjusted}


def test_预算回执精确_覆盖分支同一条(budgets):
    body = budgets["b1"]
    assert list(body.keys()) == ["id", "amount", "adjusted"]
    assert body == {"id": body["id"], "amount": 10000, "adjusted": False}
    assert type(body["amount"]) is int
    # 同机构同年同类别重复编制按调整覆盖：同 id、adjusted=true
    assert budgets["adjusted"] == {"id": body["id"], "amount": 8000.5, "adjusted": True}
    assert isinstance(budgets["adjusted"]["amount"], float)


def test_预算执行对比精确_执行率两种取值(client, admin, base, budgets):
    body = client.get(
        f"/api/mgmt/budgets/execution?org_id={base['org1']['id']}&year=2026", headers=admin
    ).json()
    assert list(body.keys()) == ["org_id", "year", "income", "expense"]
    assert list(body["income"].keys()) == ["budget", "actual", "execution_pct"]
    assert body == {
        "org_id": base["org1"]["id"],
        "year": "2026",
        "income": {"budget": 8000.5, "actual": 2000, "execution_pct": 25.0},
        # 无支出预算：budget 兜底 0.0，execution_pct 是**值为 null 的恒在键**（不是条件键）
        "expense": {"budget": 0.0, "actual": 500.5, "execution_pct": None},
    }
    assert type(body["income"]["actual"]) is int
    assert isinstance(body["income"]["execution_pct"], float)
    assert isinstance(body["expense"]["budget"], float)
    # 全零分支：coalesce(sum, 0.0) 恒 float
    empty = client.get(
        f"/api/mgmt/budgets/execution?org_id={base['org1']['id']}&year=2025", headers=admin
    ).json()
    assert empty == {
        "org_id": base["org1"]["id"],
        "year": "2025",
        "income": {"budget": 0.0, "actual": 0.0, "execution_pct": None},
        "expense": {"budget": 0.0, "actual": 0.0, "execution_pct": None},
    }
    assert isinstance(empty["income"]["actual"], float)


# ---------------------------------------------------------------- 物资出入库


@pytest.fixture(scope="module")
def movements(client, admin, base):
    asset = client.post(
        "/api/mgmt/assets",
        json={"org_id": base["org1"]["id"], "code": "MGT-ZC-001", "name": "轮椅",
              "category": "equipment", "quantity": 10},
        headers=admin,
    ).json()
    m1 = client.post(
        f"/api/mgmt/assets/{asset['id']}/movements",
        json={"movement_type": "inbound", "quantity": 5, "note": "采购入库"},
        headers=admin,
    )
    assert m1.status_code == 201, m1.text
    m2 = client.post(
        f"/api/mgmt/assets/{asset['id']}/movements",
        json={"movement_type": "issue", "quantity": 3},
        headers=admin,
    ).json()
    return {"asset": asset, "m1": m1.json(), "m2": m2}


def test_出入库回执精确(movements):
    body = movements["m1"]
    assert list(body.keys()) == ["id", "asset_id", "movement_type", "asset_quantity", "asset_status"]
    assert body == {
        "id": body["id"],
        "asset_id": movements["asset"]["id"],
        "movement_type": "inbound",
        "asset_quantity": 15,
        "asset_status": "in_use",
    }
    assert movements["m2"] == {
        "id": movements["m2"]["id"],
        "asset_id": movements["asset"]["id"],
        "movement_type": "issue",
        "asset_quantity": 12,
        "asset_status": "in_use",
    }


def test_出入库流水清单精确(client, admin, movements):
    rows = client.get(f"/api/mgmt/assets/{movements['asset']['id']}/movements", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [MOVEMENT_ROW_KEYS] * 2
    assert rows == [
        {"id": movements["m1"]["id"], "movement_type": "inbound", "quantity": 5,
         "note": "采购入库", "at": rows[0]["at"]},
        {"id": movements["m2"]["id"], "movement_type": "issue", "quantity": 3,
         "note": "", "at": rows[1]["at"]},
    ]  # id 正序
    assert all(isinstance(r["at"], str) for r in rows)


# ---------------------------------------------------------------- 系统参数


@pytest.fixture(scope="module")
def params(client, admin):
    first = client.post(
        "/api/mgmt/params", json={"key": "portal.notice", "value": "今日停电"}, headers=admin
    )
    assert first.status_code == 200, first.text
    updated = client.post(
        "/api/mgmt/params",
        json={"key": "portal.notice", "value": "恢复供电", "description": "门户公告"},
        headers=admin,
    ).json()
    other = client.post(
        "/api/mgmt/params", json={"key": "app.version", "value": "1.0.0"}, headers=admin
    ).json()
    return {"first": first.json(), "updated": updated, "other": other}


def test_参数回执与清单精确(client, admin, params):
    assert list(params["first"].keys()) == ["key", "value"]
    assert params["first"] == {"key": "portal.notice", "value": "今日停电"}
    assert params["updated"] == {"key": "portal.notice", "value": "恢复供电"}
    assert params["other"] == {"key": "app.version", "value": "1.0.0"}
    rows = client.get("/api/mgmt/params", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [PARAM_ROW_KEYS] * 2
    assert rows == [
        {"key": "app.version", "value": "1.0.0", "description": "", "updated_at": rows[0]["updated_at"]},
        {"key": "portal.notice", "value": "恢复供电", "description": "门户公告",
         "updated_at": rows[1]["updated_at"]},
    ]  # 按 key 排序
    assert all(isinstance(r["updated_at"], str) for r in rows)
