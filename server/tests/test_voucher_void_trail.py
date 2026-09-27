"""记账凭证作废只翻一个状态：谁作废的、何时、为什么都不留（P2-522，第九批「留痕承诺」扫描 W3-9）。

会计模块的注释写着「冲销走作废……账务留痕的意义就在于错了也要看得见」；`POST /api/accounting/vouchers/{id}/void`
原先只 `voucher.status = "void"`——凭证作废之后试算平衡表里少了这笔账，查账时只看得到一张「已作废」的凭证。页面上
`confirm()` 一下就作废，连原因都没处填。

修法：凭证加作废人、作废时间、作废原因三列（迁移 c7e9a1b3d5f8，存量不回填）；作废接口收可选的原因（原先不带请求体的
调用方照旧能作废），翻状态与写留痕同一条带条件的 UPDATE；已作废凭证的详情带出留痕；页面改成页内表单、原因必填。
"""
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2522 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    assert client.post("/api/users", headers=admin, json={
        "username": "p2522_dir", "password": "passw0rd1", "full_name": "P2522 财务科长", "role": "director",
        "org_id": org}).status_code in (200, 201)
    token = client.post("/api/auth/login", json={"username": "p2522_dir", "password": "passw0rd1"}).json()
    return {"org": org, "director": {"Authorization": f"Bearer {token['access_token']}"}, "n": 0}


def _posted(client, world):
    world["n"] += 1
    voucher = client.post("/api/accounting/vouchers", headers=world["director"], json={
        "org_id": world["org"], "voucher_no": f"P2522-{world['n']}", "voucher_date": "2026-09-01",
        "entries": [{"subject_code": "1001", "debit": 100}, {"subject_code": "4004", "credit": 100}]})
    assert voucher.status_code == 201, voucher.text
    vid = voucher.json()["id"]
    assert client.post(f"/api/accounting/vouchers/{vid}/post", headers=world["director"]).status_code == 200
    return vid


def test_作废留下作废人时间原因_详情看得到(client, world):
    vid = _posted(client, world)
    resp = client.post(f"/api/accounting/vouchers/{vid}/void", headers=world["director"],
                       json={"reason": "科目记错，已另开更正凭证"})
    assert resp.status_code == 200 and resp.json() == {"id": vid, "status": "void"}, resp.text
    detail = client.get(f"/api/accounting/vouchers/{vid}", headers=world["director"]).json()
    assert (detail["voided_by_name"], detail["void_reason"]) == ("P2522 财务科长", "科目记错，已另开更正凭证")   # 修前无此键
    assert detail["voided_at"]
    again = client.post(f"/api/accounting/vouchers/{vid}/void", headers=world["director"], json={"reason": "再作废一次"})
    assert again.status_code == 409, again.text
    assert client.get(f"/api/accounting/vouchers/{vid}", headers=world["director"]).json()["void_reason"] == "科目记错，已另开更正凭证"
    # 清单不带留痕（条件键，只在已作废凭证的详情里）
    rows = client.get("/api/accounting/vouchers", headers=world["director"], params={"period": "2026-09"}).json()
    assert "void_reason" not in next(r for r in rows if r["id"] == vid)


def test_不带请求体的老调用方照旧能作废(client, world):
    vid = _posted(client, world)
    resp = client.post(f"/api/accounting/vouchers/{vid}/void", headers=world["director"])
    assert resp.status_code == 200, resp.text
    detail = client.get(f"/api/accounting/vouchers/{vid}", headers=world["director"]).json()
    assert (detail["voided_by_name"], detail["void_reason"]) == ("P2522 财务科长", "")


def test_页面作废要填原因_详情显示留痕():
    source = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    start = source.index("async function renderAccounting(")
    body = source[start:source.index("\nasync function ", start + 1)]
    assert 'confirm("作废该凭证？")' not in body   # 修前 confirm() 一下就作废，原因无处可填
    void = body[body.index("else if (d.void) {"):body.index("else if (d.detail) {")]
    assert 'spdModal("作废凭证"' in void and 'name: "reason"' in void and "required: true" in void
    assert "JSON.stringify({ reason: form.reason })" in void
    detail = body[body.index("else if (d.detail) {"):]
    for key in ("v.voided_by_name", "v.voided_at", "v.void_reason"):
        assert f"esc({key}" in detail or f"esc(({key}" in detail, key
