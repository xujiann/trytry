"""人事页「月度薪酬」按期间取数（P2-853，第二十三批「部分之和 vs 整体」扫描 Y4-3；P1-149 的补充事实）。

`list_payroll` 不带 `period` 时，「合计发放」在库里对**全部期间**求和、清单截到 500 行；人事页从来不带期间——面板叫
「月度薪酬」，P1-149 的修法注释与登记都把这一行当「全县一个月发薪」的合计，实际却是开账以来所有月份的总和（3 名职工 ×
3 个月、每人每月 5000，页面印 45000）。修后页面按期间取（缺省最近一个有记录的月份，可切换），合计写成「YYYY-MM 合计发放」；
接口缺省行为不动。
"""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")


def _render():
    start = PAGE.index("async function renderHrFinance()")
    return PAGE[start:PAGE.index("\nasync function ", start + 10)]


def test_页面按期间取数_合计写明期间():
    body = _render()
    assert "api(`/api/mgmt/payroll?period=${encodeURIComponent(payPeriod)}`)" in body   # 修前只调不带期间的
    assert "all.records.map((r) => r.period).sort().pop()" in body   # 缺省最近一个有记录的月份
    assert '${payPeriod ? `${esc(payPeriod)} ` : ""}合计发放' in body
    assert '<form class="inline" id="pay-filter">' in body and "PAYROLL_PERIOD = e.target.period.value;" in body


def test_带期间的合计只算当月(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2853 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    employee = client.post("/api/mgmt/employees", headers=admin, json={
        "org_id": org, "name": "P2853 职工", "title": "医师"})
    assert employee.status_code in (200, 201), employee.text
    for period in ("2031-07", "2031-08"):
        made = client.post("/api/mgmt/payroll", headers=admin, json={
            "employee_id": employee.json()["id"], "period": period, "base_salary": 5000})
        assert made.status_code in (200, 201), made.text
    month = client.get("/api/mgmt/payroll", headers=admin, params={"period": "2031-08"}).json()
    assert month["total_amount"] == 5000.0 and [r["period"] for r in month["records"]] == ["2031-08"]
