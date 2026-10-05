"""人员下沉台账能按「待补职称等级」筛出下沉指标里报的那几人次（P2-1314，第三十八批扫描 AB1-5）。

下沉指标（`GET /api/staffing/dispatch-stats`）把「长期派驻当年满半年、职称等级未填」的单独报成 `unknown_title_level`，人员下沉页
据此提示「……职称等级未维护……请在下方台账补齐等级」；可台账（`GET /api/staffing/secondments`）按编号倒序、页面只取 100 条，
筛选只有接收机构 / 分组 / 派驻类型 / 是否在派四个——要补的恰是满半年的长期派驻、必然是早建的行，第一批被挤出去，国家监测指标
「中级及以上医师派驻 6 个月以上人数」就一直少数这些人次。修前实测（scan38 ab1/r6）：unknown_title_level=1；台账 100 行、
X-Total-Count=101，那条满半年、等级未填的 #1 不在。

修法：台账加 `needs_level` 筛选，判据与 `dispatch_stats` 计 `unknown_title_level` 那一句抽成同一组帮手（`_rows_in_year` 截年度内
天数、`_needs_title_level` 判这一类）；提示条带一个按钮，点了台账按 `needs_level=true` 重取。
"""
import ast
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert

from app.database import SessionLocal
from app.models import Employee, Secondment
from conftest import freeze_business_date

SERVER = Path(__file__).resolve().parent.parent
STATIC = SERVER / "app" / "static"
#: 「当年满半年」随日历变：业务日期冻在这一天，逐条可复算
TODAY = date(2026, 10, 4)


def _ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


#: (说明, 职称等级, 派驻类型, 开始, 结束, 接收机构, 算不算待补等级)
CASES = [
    ("满半年 · 未填", "none", "long_term", _ago(200), "", "town", True),
    ("满半年 · 未填 · 已结束", "none", "long_term", "2026-01-01", "2026-08-01", "west", True),   # 年内 212 天
    ("满半年 · 中级", "intermediate", "long_term", _ago(200), "", "town", False),
    ("满半年 · 初级", "junior", "long_term", _ago(200), "", "town", False),   # 不是中级及以上、但等级填了
    ("不满半年 · 未填", "none", "long_term", _ago(100), "", "town", False),
    ("巡诊满半年 · 未填", "none", "rounds", _ago(200), "", "town", False),
    ("跨年 · 年内不满半年 · 未填", "none", "long_term", "2025-03-01", "2026-03-01", "town", False),
    ("去年整段 · 未填", "none", "long_term", "2025-01-01", "2025-12-01", "town", False),
    ("日期非法 · 未填", "none", "long_term", "2026-02-30", "", "town", False),   # 单独报 invalid_date_records
]


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {}
    for key, name, level in (("county", "P21314 县人民医院", "county"), ("town", "P21314 东镇卫生院", "township"),
                             ("west", "P21314 西镇卫生院", "township")):
        resp = client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township" if level == "township" else "lead_hospital", "level": level})
        assert resp.status_code == 201, resp.text
        orgs[key] = resp.json()["id"]
    ids = {}
    with SessionLocal() as db:
        for label, level, kind, start, end, to_org, _needs in CASES:
            employee = Employee(org_id=orgs["county"], name=f"P21314 {label}", title="主治医师", title_level=level)
            db.add(employee)
            db.flush()
            row = Secondment(employee_id=employee.id, from_org_id=orgs["county"], to_org_id=orgs[to_org],
                             start_date=start, end_date=end, assignment_type=kind)
            db.add(row)
            db.flush()
            ids[label] = (row.id, employee.id)
        # 之后又建了 100 条巡诊：台账页面只取最新 100 条，上面那几条早建的全被挤出去
        tour = [Employee(org_id=orgs["county"], name=f"P21314 巡诊{i}", title="医师", title_level="junior") for i in range(100)]
        db.add_all(tour)
        db.flush()
        db.execute(insert(Secondment), [{
            "employee_id": e.id, "from_org_id": orgs["county"], "to_org_id": orgs["town"], "start_date": _ago(3),
            "end_date": _ago(1), "assignment_type": "rounds"} for e in tour])
        db.commit()
    return {"orgs": orgs, "ids": ids}


def _ledger(client, admin, query: str):
    resp = client.get(f"/api/staffing/secondments?{query}", headers=admin)
    assert resp.status_code == 200, resp.text
    return [r["id"] for r in resp.json()], int(resp.headers["X-Total-Count"])


def test_待补等级的人次_台账按needs_level筛得出来(client, admin, world):
    needs = sorted((world["ids"][label][0] for label, *_rest, flag in CASES if flag), reverse=True)
    with freeze_business_date(TODAY):
        stats = client.get("/api/staffing/dispatch-stats", headers=admin).json()
        page_rows, total = _ledger(client, admin, "limit=100")   # renderStaffing 原样调用
        rows, count = _ledger(client, admin, "needs_level=true&limit=100")
        others, rest = _ledger(client, admin, "needs_level=false&limit=500")
        west, _ = _ledger(client, admin, f"needs_level=true&to_org_id={world['orgs']['west']}")
    assert stats["unknown_title_level"] == len(needs) == 2 and stats["invalid_date_records"] == 1
    assert not set(needs) & set(page_rows) and total == len(CASES) + 100   # 修前只有这一条路，那几条一条都不在
    assert count == stats["unknown_title_level"] and rows == needs   # 修前参数被忽略：X-Total-Count 是全部 109 条
    assert rest == total - count and not set(needs) & set(others)
    assert west == [world["ids"]["满半年 · 未填 · 已结束"][0]]   # 与其余筛选叠加


def test_补齐一条等级_指标与筛选一起变(client, admin, world):
    _row, employee = world["ids"]["满半年 · 未填"]
    resp = client.patch(f"/api/staffing/employees/{employee}/title-level", headers=admin, json={"title_level": "intermediate"})
    assert resp.status_code == 200, resp.text
    with freeze_business_date(TODAY):
        stats = client.get("/api/staffing/dispatch-stats", headers=admin).json()
        rows, count = _ledger(client, admin, "needs_level=true")
    assert stats["unknown_title_level"] == count == 1
    assert rows == [world["ids"]["满半年 · 未填 · 已结束"][0]]


def test_两处共用同一组帮手():
    """指标与筛选同一句判据：`dispatch_stats` 与 `list_secondments` 都经 `_rows_in_year` 取年度内天数、经 `_needs_title_level` 判。"""
    tree = ast.parse((SERVER / "app" / "routers" / "staffing.py").read_text(encoding="utf-8"))
    funcs = {fn.name: fn for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in ("dispatch_stats", "list_secondments"):
        called = {node.func.id for node in ast.walk(funcs[name])
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        assert {"_rows_in_year", "_needs_title_level"} <= called, name


def test_提示条点了台账按needs_level重取():
    source = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    start = source.index("async function renderStaffing()")
    body = source[start:source.index("\nasync function ", start + 1)]
    # 筛选对象多了统计年度、台账请求带上同一个年度（P2-1509，见 test_staffing_dispatch_year）
    assert 'const STAFFING_FILTER = { needs_level: false, year: "" };' in source
    assert ('api(`/api/staffing/secondments?limit=100${STAFFING_FILTER.needs_level ? "&needs_level=true" : ""}'
            '${yearQuery ? `&${yearQuery}` : ""}`)') in body
    prompt = body[body.index("${stats.unknown_title_level"):body.index('${table(["接收机构"')]
    assert 'data-stneeds="1"' in prompt   # 修前只是一行字，「请在下方台账补齐等级」却找不到那几条
    handler = body[body.index("$(\"#page-body\").onclick"):]
    assert 'STAFFING_FILTER.needs_level = stneeds === "1";' in handler
    assert handler.index('STAFFING_FILTER.needs_level = stneeds === "1";') < handler.index("return route();")
