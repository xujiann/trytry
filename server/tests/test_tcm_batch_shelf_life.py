"""制剂批次的缺省效期按日历月推：起算日对应 N 个月后那天的前一天（P2-165）。

建批接口写「效期缺省按配方有效期（月）自动推算」，实现按 30 天一个月折算：12 个月 = 360 天，
有效期至比应有的早 4~5 天；24 个月早 10 天、120 个月早近两个月——批次提前判过期、提前进效期预警。
药品标签有效期标注到日的，「应当为起算日期对应年月日的前一天」（《药品说明书和标签管理规定》第二十三条）。
"""
import pytest


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P2165 县中医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]


def _formula(client, admin, code, months):
    resp = client.post("/api/tcm/formulas", headers=admin, json={
        "code": code, "name": f"P2165 制剂{code}", "dosage_form": "decoction", "shelf_life_months": months})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.parametrize("code,months,produced,expected", [
    ("P2165A", 12, "2026-05-01", "2027-04-30"),   # 修前 2027-04-26（360 天）
    ("P2165B", 24, "2026-05-01", "2028-04-30"),   # 修前 2028-04-20
    ("P2165C", 1, "2026-01-31", "2026-02-27"),    # 2 月没有 31 日：取月末再往前一天；修前 2026-03-02
    ("P2165D", 6, "2027-08-15", "2028-02-14"),    # 跨年
])
def test_缺省效期按日历月推到对应日的前一天(client, admin, org, code, months, produced, expected):
    formula = _formula(client, admin, code, months)
    batch = client.post("/api/tcm/preparation-batches", headers=admin, json={
        "formula_id": formula, "batch_no": f"{code}-1", "org_id": org, "quantity": 10, "produced_date": produced})
    assert batch.status_code == 201, batch.text
    assert batch.json()["expire_date"] == expected


def test_推出的效期超出可表示年份是422不是500(client, admin, org):
    formula = _formula(client, admin, "P2165E", 120)
    batch = client.post("/api/tcm/preparation-batches", headers=admin, json={
        "formula_id": formula, "batch_no": "P2165E-1", "org_id": org, "quantity": 10, "produced_date": "9995-01-01"})
    assert batch.status_code == 422, batch.text
