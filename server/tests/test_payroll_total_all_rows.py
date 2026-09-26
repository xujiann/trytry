"""薪酬「合计发放」按筛选条件全量求和，不是只加截断后的那一页（P1-149）。

`GET /api/mgmt/payroll` 的清单截到最新 500 行（P1-49 的行数上限），合计却是在这 500 行上 `sum` 出来的：全县一个月
发薪超过 500 人，页面上唯一的那行「合计发放」就少算了第 501 人起的全部。同文件的 `finance_summary` 早就在库里求和。
"""
import pytest


@pytest.fixture(scope="module")
def employees(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1149 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ids = []
    for i in range(502):
        resp = client.post("/api/mgmt/employees", headers=admin, json={
            "org_id": org, "name": f"P1149 员工{i}", "position": "护士"})
        assert resp.status_code == 201, resp.text
        ids.append(resp.json()["id"])
    return ids


def test_合计按全部筛中的记录求和(client, admin, employees):
    for emp in employees:
        resp = client.post("/api/mgmt/payroll", headers=admin, json={
            "employee_id": emp, "period": "2026-08", "base_salary": 6000, "perf_bonus": 0, "perf_coefficient": 1.0})
        assert resp.status_code == 201, resp.text
    body = client.get("/api/mgmt/payroll?period=2026-08", headers=admin).json()
    assert len(body["records"]) == 500                      # 清单行数上限不变（P1-49）
    assert body["total_amount"] == 502 * 6000               # 修前 500 × 6000 = 3,000,000
    assert type(body["total_amount"]) is int                # 整数金额照旧读回 int，响应字节不变
    one = client.get(f"/api/mgmt/payroll?period=2026-08&employee_id={employees[0]}", headers=admin).json()
    assert one["total_amount"] == 6000 and type(one["total_amount"]) is int
    none = client.get("/api/mgmt/payroll?period=1999-01", headers=admin).json()
    assert none == {"total_amount": 0, "records": []} and type(none["total_amount"]) is int
