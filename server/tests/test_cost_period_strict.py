"""成本核算的期间走严格校验：原先只过宽松的 `month_bounds`，`2026-9` 得一张 200 的空表（P2-340）。

`month_bounds` 按设计宽松（`2026-1` 照收，见其 docstring），它只负责把期间展开成日期区间；而科室成本汇总与单位成本
接着按 `DepartmentCost.period == period` 比字符串，库里的期间是规范的 `YYYY-MM`（`CostIn.period` 是 PeriodStr）——
`2026-9` 一条也对不上：汇总是空表，单位成本按日期区间照数 9 月的人次与床日、成本却是 0，诊次成本 0.00。
成本页的切换框是自由文本、由后端判合法与否，判过的值存进本地——下次进来还是那张空表。

修法：两处先过 `require_month`（与试算平衡表 P1-62 同一个口径），`month_bounds` 只管日期区间。
另钉一道零基线闸门：只经 `month_bounds` 校验的期间，不许再拿去按字符串比。
"""
import ast
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
#: 严格校验期间形状的守卫（与 `test_periodstr_single_source.MONTH_GUARDS` 相比去掉了宽松的 month_bounds）
STRICT = {"require_month", "period_bounds", "check_assess_period", "_period_range"}


@pytest.fixture(scope="module")
def org(client, admin):
    got = client.post("/api/organizations", headers=admin,
                      json={"name": "P2340 成本卫生院", "org_type": "township", "level": "township"})
    assert got.status_code == 201, got.text
    return got.json()["id"]


@pytest.mark.parametrize("path", ["/api/cost/departments", "/api/cost/unit-cost"])
def test_期间不补零_422而不是空表(client, admin, org, path):
    got = client.get(path, headers=admin, params={"period": "2026-9", "org_id": org})
    assert got.status_code == 422, got.text   # 修前 200：空表 / 成本 0
    assert got.json() == {"detail": "period 格式须为 YYYY-MM"}
    assert client.get(path, headers=admin, params={"period": "2026-09", "org_id": org}).status_code == 200


def test_月份不存在_422(client, admin, org):
    got = client.get("/api/cost/departments", headers=admin, params={"period": "2026-13"})
    assert got.status_code == 422, got.text
    assert got.json() == {"detail": "period 2026-13 不存在（月份须为 01~12）"}


# ================================================================ 零基线闸门
def _loose_period_compared_as_string() -> list[str]:
    """把 `period` 交给 `month_bounds`、没交给任何严格守卫、却拿它做 `==` 比较的函数。"""
    hits = []
    for path in sorted(APP.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            guards = {n.func.id for n in ast.walk(fn)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                      and any(isinstance(a, ast.Name) and a.id == "period" for a in n.args)}
            if "month_bounds" not in guards or guards & STRICT:
                continue
            compared = any(
                isinstance(n, ast.Compare) and any(isinstance(op, ast.Eq) for op in n.ops)
                and any(isinstance(side, ast.Name) and side.id == "period" for side in [n.left, *n.comparators])
                for n in ast.walk(fn)
            )
            if compared:
                hits.append(f"{path.relative_to(APP).as_posix()}::{fn.name}")
    return hits


def test_只经宽松校验的期间不按字符串比():
    hits = _loose_period_compared_as_string()
    assert hits == [], (
        "`month_bounds` 宽松（`2026-9` 照收），按 `== period` 比字符串就是一个永远为空、却不报错的结果集——"
        "先过 `require_month`：\n  " + "\n  ".join(hits)
    )


def test_判据自证(tmp_path, monkeypatch):
    (tmp_path / "probe.py").write_text(
        "def loose(period, db):\n"                       # 该命中
        "    month_bounds(period)\n"
        "    return db.query(T).filter(T.period == period)\n"
        "def strict(period, db):\n"                      # 先过了严格校验，不算
        "    period = require_month(period)\n"
        "    start, end = month_bounds(period)\n"
        "    return db.query(T).filter(T.period == period)\n"
        "def ranged(period, db):\n"                      # 只用日期区间，不算
        "    start, end = month_bounds(period)\n"
        "    return db.query(T).filter(T.at >= start)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "APP", tmp_path)
    assert _loose_period_compared_as_string() == ["probe.py::loose"]
