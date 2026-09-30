"""预算执行率先取整到两位再按「> 100」判红：超支 40 元显示绿色 100.0%（P2-990，第二十八批「取整与精度发生在哪一步」扫描
F2-4）。

`admin_mgmt` 的预算执行接口返回 `round(actual * 100 / budget, 2)`，页面按 `execution_pct > 100` 判红：支出预算 1,000,000、
实际 1,000,040，执行率 100.0、标签绿色；预算到千万级时，四百多元以内的超支都显示绿色。同一行里就有预算与实际的原值，红标的
意思就是实际超预算。

修法：红标按原值判（`d.actual > d.budget`），执行率照旧显示两位。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")


def _row_template() -> str:
    start = PAGE.index('$("#bud-exec").innerHTML = table(')
    return PAGE[start:PAGE.index("</tr>`);", start)]


def test_红标按实际与预算的原值判():
    row = _row_template()
    assert 'd.actual > d.budget ? "red" : "green"' in row
    assert "d.execution_pct > 100" not in row   # 修前按取整后的执行率判


@pytest.fixture(scope="module")
def execution(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2990 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    budget = client.post("/api/mgmt/budgets", headers=admin, json={
        "org_id": org, "year": "2026", "category": "expense", "amount": 1000000})
    assert budget.status_code in (200, 201), budget.text
    entry = client.post("/api/mgmt/finance", headers=admin, json={
        "org_id": org, "period": "2026-05", "category": "expense", "item": "P2990 药品采购", "amount": 1000040})
    assert entry.status_code in (200, 201), entry.text
    resp = client.get("/api/mgmt/budgets/execution", headers=admin, params={"org_id": org, "year": "2026"})
    assert resp.status_code == 200, resp.text
    return resp.json()["expense"]


def test_超支40元_执行率显示100但原值超了(execution):
    assert (execution["budget"], execution["actual"], execution["execution_pct"]) == (1000000, 1000040, 100.0)


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端判断")
def test_跑一遍页面的判断_超支标红(execution):
    script = ("const d = JSON.parse(process.argv[1]);"
              "console.log(d.actual > d.budget ? 'red' : 'green');")
    out = subprocess.run(["node", "-e", script, json.dumps(execution)], capture_output=True, text=True, check=True,
                         timeout=60).stdout.strip()
    assert out == "red"   # 修前按 execution_pct > 100 判 green
