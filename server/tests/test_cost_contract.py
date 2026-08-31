"""成本核算 `/api/cost` 五个端点的**特征化网 + 响应契约**。

套路同 test_rules_contract.py / test_admin_mgmt_contract.py：先钉住**当前**响应的
完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。

本簇的建模判断（Money/Float 混布的重点模块，判据是列类型不是字段名）：

- `DepartmentCost.amount` 是 **Money** 列：归集回执经 `upsert_unique` 的
  `db.refresh` 读回，整数金额是 **int**（2000），带小数才是 float（500.5）
  → `int | float`（陷阱一，此处以 type 断言钉死）。
- `CostAllocationRule.ratio_pct` 是 **Float** 列：整数比例读回来就是 60.0
  → 声明 `float` 才是原样，与同模块的 Money 相反。
- 科室汇总里 `direct_cost`/`by_type`/`total_cost` 恒 float——桶从字面量 0.0
  起加（`{t: 0.0}`），int 金额加进来也是 float；而 `allocated_in`/
  `allocated_out`/`unallocated_ratio_amount` 的**无分摊兜底是字面量 int 0**
  （`.get(dept_id, 0)`），有分摊才是 round 出来的 float → `int | float`。
  同一行里两族并存，本文件两侧取值各钉一遍。
- `unit-cost` 的 `total_cost` 是 Money 求和：空期间 `sum([])` 是 int 0，
  有数是 int/float 原样 → `int | float`；其余单位成本字段全部经真除法或
  字面量 0.0 → 恒 float。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

UPSERT_KEYS = ["id", "updated", "amount"]
RULE_RECEIPT_KEYS = ["id", "from_dept_id", "to_dept_id", "ratio_pct"]
RULE_ROW_KEYS = ["id", "org_id", "from_dept_id", "to_dept_id", "ratio_pct"]
SUMMARY_ROW_KEYS = [
    "dept_id", "dept_name", "dept_category", "org_name", "direct_cost", "by_type",
    "allocated_in", "allocated_out", "total_cost", "unallocated_ratio_amount",
]
BY_TYPE_KEYS = ["labor", "drug", "consumable", "depreciation", "overhead"]
UNIT_COST_KEYS = [
    "period", "org_id", "total_cost", "outpatient_visits", "occupied_bed_days",
    "outpatient_cost", "inpatient_cost", "cost_per_visit", "cost_per_bed_day",
]


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def base(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "成本契约医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    clinical = client.post(
        "/api/mgmt/departments",
        json={"org_id": org["id"], "code": "NK", "name": "内科"},
        headers=admin,
    ).json()
    logistics = client.post(
        "/api/mgmt/departments",
        json={"org_id": org["id"], "code": "HQ", "name": "后勤", "category": "admin"},
        headers=admin,
    ).json()
    return {"org": org, "clinical": clinical, "logistics": logistics}


@pytest.fixture(scope="module")
def seeded(client, admin, base):
    """内科整数人员经费 2000 + 后勤小数运行费 500.5，后勤 60% 分摊给内科。"""
    labor = client.post(
        "/api/cost/departments",
        json={"dept_id": base["clinical"]["id"], "period": "2026-07",
              "cost_type": "labor", "amount": 2000},
        headers=admin,
    )
    assert labor.status_code == 201, labor.text
    overhead = client.post(
        "/api/cost/departments",
        json={"dept_id": base["logistics"]["id"], "period": "2026-07",
              "cost_type": "overhead", "amount": 500.5},
        headers=admin,
    )
    assert overhead.status_code == 201, overhead.text
    rule = client.post(
        "/api/cost/allocation-rules",
        json={"from_dept_id": base["logistics"]["id"], "to_dept_id": base["clinical"]["id"],
              "ratio_pct": 60},
        headers=admin,
    )
    assert rule.status_code == 201, rule.text
    return {"labor": labor.json(), "overhead": overhead.json(), "rule": rule.json()}


def test_归集回执精确_Money整数是int(client, admin, base, seeded):
    body = seeded["labor"]
    assert list(body.keys()) == UPSERT_KEYS
    assert body == {"id": body["id"], "updated": False, "amount": 2000}
    # Money 列 refresh 读回：整数是 int，声明 float 会把 2000 变 2000.0
    assert type(body["amount"]) is int
    assert seeded["overhead"] == {"id": seeded["overhead"]["id"], "updated": False, "amount": 500.5}
    assert isinstance(seeded["overhead"]["amount"], float)
    # 同键重复归集按覆盖：同 id、updated=True
    overwrite = client.post(
        "/api/cost/departments",
        json={"dept_id": base["clinical"]["id"], "period": "2026-07",
              "cost_type": "labor", "amount": 2000},
        headers=admin,
    ).json()
    assert overwrite == {"id": body["id"], "updated": True, "amount": 2000}


def test_分摊规则回执与清单精确_Float列恒float(client, admin, base, seeded):
    body = seeded["rule"]
    assert list(body.keys()) == RULE_RECEIPT_KEYS
    assert body == {
        "id": body["id"],
        "from_dept_id": base["logistics"]["id"],
        "to_dept_id": base["clinical"]["id"],
        "ratio_pct": 60.0,
    }
    # ratio_pct 是 Float 列：整数入参 60 读回就是 60.0——声明 int | float 反而没意义
    assert isinstance(body["ratio_pct"], float)
    rows = client.get("/api/cost/allocation-rules", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [RULE_ROW_KEYS]
    assert rows == [{
        "id": body["id"],
        "org_id": base["org"]["id"],
        "from_dept_id": base["logistics"]["id"],
        "to_dept_id": base["clinical"]["id"],
        "ratio_pct": 60.0,
    }]
    assert client.get(
        f"/api/cost/allocation-rules?org_id={base['org']['id']}", headers=admin
    ).json() == rows


def test_科室成本汇总精确_两族数值并存(client, admin, base, seeded):
    rows = client.get(
        f"/api/cost/departments?period=2026-07&org_id={base['org']['id']}", headers=admin
    ).json()
    assert [list(r.keys()) for r in rows] == [SUMMARY_ROW_KEYS] * 2
    assert [list(r["by_type"].keys()) for r in rows] == [BY_TYPE_KEYS] * 2
    clinical_row, logistics_row = rows  # 按 total_cost 倒序：内科在前
    assert rows == [
        {
            "dept_id": base["clinical"]["id"],
            "dept_name": "内科",
            "dept_category": "clinical",
            "org_name": "成本契约医院",
            "direct_cost": 2000.0,
            "by_type": {"labor": 2000.0, "drug": 0.0, "consumable": 0.0,
                        "depreciation": 0.0, "overhead": 0.0},
            "allocated_in": 300.3,
            "allocated_out": 0,
            "total_cost": 2300.3,
            "unallocated_ratio_amount": 0,
        },
        {
            "dept_id": base["logistics"]["id"],
            "dept_name": "后勤",
            "dept_category": "admin",
            "org_name": "成本契约医院",
            "direct_cost": 500.5,
            "by_type": {"labor": 0.0, "drug": 0.0, "consumable": 0.0,
                        "depreciation": 0.0, "overhead": 500.5},
            "allocated_in": 0,
            "allocated_out": 300.3,
            "total_cost": 200.2,
            "unallocated_ratio_amount": 200.2,
        },
    ]
    # 桶从 0.0 起加：direct_cost/by_type 恒 float（整数 2000 出参是 2000.0）；
    # 无分摊侧 .get(id, 0) 兜底是 int 0，有分摊才是 float —— int | float 两侧各钉
    assert isinstance(clinical_row["direct_cost"], float)
    assert isinstance(clinical_row["by_type"]["labor"], float)
    assert isinstance(clinical_row["allocated_in"], float)
    assert type(clinical_row["allocated_out"]) is int
    assert type(clinical_row["unallocated_ratio_amount"]) is int
    assert type(logistics_row["allocated_in"]) is int
    assert isinstance(logistics_row["allocated_out"], float)
    assert isinstance(logistics_row["unallocated_ratio_amount"], float)
    assert client.get("/api/cost/departments?period=1999-01", headers=admin).json() == []


def test_单位成本精确_总额int或float其余恒float(client, admin, base, seeded):
    body = client.get(
        f"/api/cost/unit-cost?period=2026-07&org_id={base['org']['id']}", headers=admin
    ).json()
    assert list(body.keys()) == UNIT_COST_KEYS
    assert body == {
        "period": "2026-07",
        "org_id": base["org"]["id"],
        "total_cost": 2500.5,
        "outpatient_visits": 0,
        "occupied_bed_days": 0,
        "outpatient_cost": 0.0,
        "inpatient_cost": 0.0,
        "cost_per_visit": 0.0,
        "cost_per_bed_day": 0.0,
    }
    assert isinstance(body["total_cost"], float)
    # 空期间：Money 求和 sum([]) 是字面量 int 0；单位成本兜底 0.0 恒 float
    empty = client.get(
        f"/api/cost/unit-cost?period=1999-01&org_id={base['org']['id']}", headers=admin
    ).json()
    assert empty == {
        "period": "1999-01",
        "org_id": base["org"]["id"],
        "total_cost": 0,
        "outpatient_visits": 0,
        "occupied_bed_days": 0,
        "outpatient_cost": 0.0,
        "inpatient_cost": 0.0,
        "cost_per_visit": 0.0,
        "cost_per_bed_day": 0.0,
    }
    assert type(empty["total_cost"]) is int
    assert isinstance(empty["outpatient_cost"], float)
    assert isinstance(empty["cost_per_visit"], float)


def test_单位成本_有门诊量分支精确(client, admin, base, seeded):
    """诊次分母来自当期就诊：用**当前月**归集 + 现挂一次门诊，除法分支全为 float。"""
    from datetime import date

    period = date.today().strftime("%Y-%m")
    resp = client.post(
        "/api/cost/departments",
        json={"dept_id": base["clinical"]["id"], "period": period,
              "cost_type": "drug", "amount": 300},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    patient = client.post(
        "/api/patients", json={"name": "成本契约患者", "id_card": "330424199007071234"},
        headers=admin,
    ).json()
    enc = client.post(
        "/api/encounters",
        json={"patient_id": patient["id"], "org_id": base["org"]["id"],
              "encounter_type": "outpatient"},
        headers=admin,
    )
    assert enc.status_code == 201, enc.text
    body = client.get(
        f"/api/cost/unit-cost?period={period}&org_id={base['org']['id']}", headers=admin
    ).json()
    assert body == {
        "period": period,
        "org_id": base["org"]["id"],
        "total_cost": 300,
        "outpatient_visits": 1,
        "occupied_bed_days": 0,
        "outpatient_cost": 300.0,
        "inpatient_cost": 0.0,
        "cost_per_visit": 300.0,
        "cost_per_bed_day": 0.0,
    }
    # Money 整数总额是 int；权重拆分与单价经真除法恒 float（300 → 300.0）
    assert type(body["total_cost"]) is int
    assert isinstance(body["outpatient_cost"], float)
    assert isinstance(body["cost_per_visit"], float)
