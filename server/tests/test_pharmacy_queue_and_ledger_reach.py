"""集中审方的待审队列、药房的批次台账不再只看「前 200 条」（P1-148）。

两处页面都只取一页（`GET /api/prescriptions` 最新 200 张、`GET /api/pharmacy/batches?limit=200` 前 200 个批次），
能操作的按钮只摆在这一页里：
- 审方：全县一小时开一百多张方，早上压着没审的那张到上午就被后开的方挤出窗口——铃铛还在数它，页面上却没有
  一行能「通过 / 退回」，发药只认审过的（`dispense` 对待药师审 409），这张方就卡死了。现在待审的单独取一遍、
  排在队列最前。
- 批次台账：第 201 个起的批次在页面上召不回、也查不出「发给了谁」。召回总是冲着某个药、某个批号去的，
  台账加上按药品编码 / 批号查（接口早就支持这两个筛选）。
"""
import os

import pytest

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def _function(name: str) -> str:
    with open(os.path.join(STATIC, "core.js"), encoding="utf-8") as fh:
        source = fh.read()
    start = source.index(f"async function {name}()")
    return source[start:source.index("\nasync function ", start + 1)]


def test_待审处方单独取一遍_排在队列最前():
    body = _function("renderRx")
    assert 'api("/api/prescriptions?status=pending_review")' in body     # 修前只取最新 200 张
    assert "const prescriptions = [...pending, ...recent.filter((p) => !pendingIds.has(p.id))];" in body
    assert 'p.status === "pending_review"' in body and "data-approve" in body   # 审方按钮仍按行状态给


def test_批次台账可按药品编码与批号查():
    body = _function("renderPharmacy")
    form = body[body.index('id="batch-filter"'):]
    form = form[:form.index("</form>")]
    assert 'name="drug_code"' in form and 'name="batch_no"' in form        # 修前没有查询入口
    handler = body[body.index('$("#batch-filter").onsubmit'):]
    handler = handler[:handler.index('$("#page-body").onclick')]
    assert "api(`/api/pharmacy/batches?${q.toString()}`)" in handler
    assert '$("#batch-ledger").innerHTML = batchTable(batches)' in handler


def test_召回回执报召回前的可发余量():
    body = _function("renderPharmacy")
    assert "退出可用汇总 ${done.available} → 0" not in body               # 召回之后 available 恒 0，修前永远「0 → 0」
    assert "退出可用汇总 ${batch ? batch.available" in body


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P1148 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]


def test_批次接口按药品编码与批号筛(client, admin, org):
    for code, batch_no in (("J01CA04", "AMX-2409"), ("J01CA04", "AMX-2410"), ("C08CA01", "AML-0101")):
        resp = client.post("/api/pharmacy/batches", headers=admin, json={
            "org_id": org, "drug_code": code, "drug_name": code, "batch_no": batch_no,
            "expire_date": "2028-06-30", "quantity": 10})
        assert resp.status_code == 201, resp.text
    by_no = client.get("/api/pharmacy/batches", headers=admin, params={"batch_no": "AMX-2410", "limit": 200}).json()
    assert [(b["drug_code"], b["batch_no"]) for b in by_no] == [("J01CA04", "AMX-2410")]
    by_code = client.get("/api/pharmacy/batches", headers=admin, params={"drug_code": "J01CA04", "limit": 200}).json()
    assert sorted(b["batch_no"] for b in by_code) == ["AMX-2409", "AMX-2410"]
