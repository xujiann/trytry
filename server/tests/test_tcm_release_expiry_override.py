"""中药制剂批次放行的效期管控跟着 `?today=` 往回拨：带一个早于效期的日期，过期批次照样放出去（P2-286）。

`release_batch` 的 docstring 写「过期批次禁止发放（效期管控）」，判定用的却是 `resolve_business_date(today)`——`today` 覆盖
按接口对接规范「仅限测试与管理排查用途，生产对接方不得传入」，实现不设限。药剂 / 经办 `POST …/release?today=2020-01-01`，
去年就过期的批次当场「已发放」（修前实测 200）。

修法：效期管控取覆盖日期与真实业务日期里晚的那个——覆盖只许把日子往后拨（判得更严），不许往回拨。
"""
from datetime import date

import pytest
from conftest import freeze_business_date

B = "/api/tcm"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2286 中医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    formula = client.post(f"{B}/formulas", headers=admin, json={"code": "P2286-F", "name": "P2286 合剂"})
    assert formula.status_code == 201, formula.text
    return {"org": org, "formula": formula.json()["id"], "n": 0}


def _batch(client, admin, world, produced, expire):
    world["n"] += 1
    batch = client.post(f"{B}/preparation-batches", headers=admin, json={
        "formula_id": world["formula"], "batch_no": f"P2286-{world['n']}", "org_id": world["org"], "quantity": 10,
        "produced_date": produced, "expire_date": expire})
    assert batch.status_code == 201, batch.text
    return batch.json()["id"]


def test_过期批次带早于效期的日期_照样禁止发放(client, admin, world):
    with freeze_business_date(date(2026, 9, 26)):
        batch = _batch(client, admin, world, "2025-01-01", "2025-06-30")
        got = client.post(f"{B}/preparation-batches/{batch}/release", params={"today": "2025-03-01"}, headers=admin)
    assert got.status_code == 409, got.text   # 修前 200：去年就过期的批次放了出去
    assert got.json()["detail"] == "批次已过效期，禁止发放"


def test_覆盖往后拨照样生效_判得更严(client, admin, world):
    with freeze_business_date(date(2026, 9, 26)):
        batch = _batch(client, admin, world, "2026-09-01", "2026-10-31")
        later = client.post(f"{B}/preparation-batches/{batch}/release", params={"today": "2026-11-15"}, headers=admin)
        assert later.status_code == 409, later.text   # 按往后拨的日子已过期
        now = client.post(f"{B}/preparation-batches/{batch}/release", headers=admin)
        assert now.status_code == 200 and now.json()["status"] == "released", now.text
