"""成本分摊：分出 + 未分摊 = 直接成本，分毫不差（P2-629，第十三批「拆分之和 vs 总额」扫描 Q3-1）。

原先逐条 `round(直接成本 × 比例, 2)`：后勤 1000.01 对半分给内外科，两边各 500.0（500.005 的浮点是 500.00499…），
比例合计 100% 的后勤科凭空剩 0.01；只分一半的，分出 500.0、未分摊 500.0，后勤总成本却是 500.01——总额拆开再加回来
对不上，与「未分摊」那一栏也对不上。改用与基金结余分配同一个最大余数法（`numtypes.split_fen`），未分摊当最后一份。
"""
import pytest

B = "/api/cost"
PERIOD = "2026-06"


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P2629 医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]


def _dept(client, admin, org, code, category="clinical"):
    resp = client.post("/api/mgmt/departments", headers=admin,
                       json={"org_id": org, "code": code, "name": code, "category": category})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _source(client, admin, org, code, amount):
    dept = _dept(client, admin, org, code, "admin")
    cost = client.post(f"{B}/departments", headers=admin,
                       json={"dept_id": dept, "period": PERIOD, "cost_type": "overhead", "amount": amount})
    assert cost.status_code == 201, cost.text
    return dept


def _rule(client, admin, src, dst, ratio):
    resp = client.post(f"{B}/allocation-rules", headers=admin,
                       json={"from_dept_id": src, "to_dept_id": dst, "ratio_pct": ratio})
    assert resp.status_code == 201, resp.text


def _rows(client, admin, org):
    rows = client.get(f"{B}/departments", headers=admin, params={"period": PERIOD, "org_id": org}).json()
    return {r["dept_id"]: r for r in rows}


def test_分满100的来源科室分到零_分出的合计等于直接成本(client, admin, org):
    hq = _source(client, admin, org, "P2629-HQ1", 1000.01)
    nk, wk = _dept(client, admin, org, "P2629-NK1"), _dept(client, admin, org, "P2629-WK1")
    _rule(client, admin, hq, nk, 50)
    _rule(client, admin, hq, wk, 50)
    rows = _rows(client, admin, org)
    assert (rows[hq]["allocated_out"], rows[hq]["total_cost"]) == (1000.01, 0.0), rows[hq]   # 修前 1000.0 / 0.01
    assert (rows[nk]["allocated_in"], rows[wk]["allocated_in"]) == (500.01, 500.0)   # 零头按规则先后补给第一条


def test_没分满的_未分摊恰是留在来源科室的那部分(client, admin, org):
    hq = _source(client, admin, org, "P2629-HQ2", 1000.01)
    _rule(client, admin, hq, _dept(client, admin, org, "P2629-NK2"), 50)
    row = _rows(client, admin, org)[hq]
    assert round(row["allocated_out"] + row["unallocated_ratio_amount"], 2) == row["direct_cost"] == 1000.01, row
    assert row["total_cost"] == row["unallocated_ratio_amount"], row   # 修前 500.01 与 500.0


def test_三方分摊的零头给余数最大的那份(client, admin, org):
    hq = _source(client, admin, org, "P2629-HQ3", 1000.01)
    targets = [_dept(client, admin, org, f"P2629-K3{i}") for i in range(3)]
    for dept, ratio in zip(targets, (33.3, 33.3, 33.4)):
        _rule(client, admin, hq, dept, ratio)
    rows = _rows(client, admin, org)
    assert [rows[d]["allocated_in"] for d in targets] == [333.0, 333.0, 334.01]   # 修前 334.0，合计 1000.0
    assert rows[hq]["total_cost"] == 0.0


def test_比例合计超100的存量照旧按各自比例分_错配照样露出来(client, admin, org):
    """P1-116 之前能建出合计超 100 的规则；存量不动，汇总照旧按各自比例分——来源科室成负数正是提示。"""
    from app.database import SessionLocal
    from app.models import CostAllocationRule

    hq = _source(client, admin, org, "P2629-HQ4", 1000)
    nk, wk = _dept(client, admin, org, "P2629-NK4"), _dept(client, admin, org, "P2629-WK4")
    with SessionLocal() as db:
        db.add_all([CostAllocationRule(org_id=org, from_dept_id=hq, to_dept_id=nk, ratio_pct=60),
                    CostAllocationRule(org_id=org, from_dept_id=hq, to_dept_id=wk, ratio_pct=60)])
        db.commit()
    rows = _rows(client, admin, org)
    assert (rows[nk]["allocated_in"], rows[wk]["allocated_in"], rows[hq]["total_cost"]) == (600.0, 600.0, -200.0)
