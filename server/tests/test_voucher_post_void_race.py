"""凭证过账与作废同时到：已作废的凭证被翻回「已过账」计入试算平衡，作废留痕被藏起来（P2-1186，第三十四批扫描 L1-7）。

作废（`void_voucher`）是带条件的 UPDATE（`WHERE status != 'void'`，P2-522）；过账（`post_voucher`）原先锁外判「草稿」、
往对象上赋值再提交，那条 UPDATE 只有 `WHERE id = ?`。过账判完还没写，作废先提交了，过账照旧把凭证整行写成「已过账」，
两路都 200：库里 status=posted 却带着作废人与作废原因，详情只在作废状态下才出作废留痕、看不到；试算平衡计入这笔，
作废重录之后同一笔收入记了两遍。

修法：过账与「还是草稿」同一条 UPDATE（`concurrency.move_row`），抢输的一路回滚、按库里的现状 409，文案与顺序请求
同一句。这里把「判过了、还没写」钉成确定的时序：过账一路取过账时刻（`utcnow`，判定之后、写入之前）的那一刻，经真实
接口插一路作废并提交。
"""
import pytest

from conftest import login

A = "/api/accounting"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21186 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    heads = {}
    for username, full_name in (("p21186_a", "P21186 财务科长"), ("p21186_b", "P21186 总会计师")):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": "director", "org_id": org, "full_name": full_name})
        assert created.status_code == 201, created.text
        heads[username] = login(client, username, "passw0rd1")
    return {"org": org, **heads}


def _draft(client, world, voucher_no, period):
    got = client.post(f"{A}/vouchers", headers=world["p21186_a"], json={
        "org_id": world["org"], "voucher_no": voucher_no, "voucher_date": f"{period}-15", "period": period,
        "summary": "收到医疗款", "entries": [{"subject_code": "1002", "debit": 50000},
                                          {"subject_code": "4001", "credit": 50000}]})
    assert got.status_code == 201 and got.json()["status"] == "draft", got.text
    return got.json()["id"]


def _trial_balance(client, world, period):
    got = client.get(f"{A}/trial-balance", headers=world["p21186_a"], params={"period": period, "org_id": world["org"]})
    assert got.status_code == 200, got.text
    return got.json()


def test_过账判完还没写时作废先提交_过账409_凭证仍是作废_试算平衡不计入(client, world, monkeypatch):
    from app.routers import accounting

    vid = _draft(client, world, "P21186-1", "2026-07")
    real, fired = accounting.utcnow, []

    def racing():
        if not fired:   # 先记上：插进来的作废自己也取作废时刻
            fired.append("作废")
            fired.append(client.post(f"{A}/vouchers/{vid}/void", headers=world["p21186_b"],
                                     json={"reason": "金额录错，作废重录"}))
        return real()

    monkeypatch.setattr(accounting, "utcnow", racing)
    got = client.post(f"{A}/vouchers/{vid}/post", headers=world["p21186_a"])
    monkeypatch.undo()
    assert fired, "插桩没有触发：过账不再取 utcnow 了，换一个判定之后、写入之前的插点"
    voided = fired[1]
    assert voided.status_code == 200 and voided.json() == {"id": vid, "status": "void"}, voided.text
    assert got.status_code == 409, got.text   # 修前 200 {'status': 'posted'}
    assert got.json() == {"detail": "当前状态 已作废 不可过账"}   # 按库里现状措辞，与顺序请求同一句
    detail = client.get(f"{A}/vouchers/{vid}", headers=world["p21186_a"]).json()
    # 修前 status=posted，详情不出作废留痕（作废人与原因还在库里、谁也看不见）
    assert (detail["status"], detail["voided_by_name"], detail["void_reason"]) == (
        "void", "P21186 总会计师", "金额录错，作废重录")
    tb = _trial_balance(client, world, "2026-07")
    assert (tb["lines"], tb["total_debit"], tb["total_credit"]) == ([], 0, 0)   # 修前 1002 借 / 4001 贷各 5 万


def test_不并发时照常过账_作废之后再过账按现状409(client, world):
    posted = _draft(client, world, "P21186-2", "2026-08")
    got = client.post(f"{A}/vouchers/{posted}/post", headers=world["p21186_a"])
    assert got.status_code == 200 and got.json() == {"id": posted, "status": "posted"}, got.text
    again = client.post(f"{A}/vouchers/{posted}/post", headers=world["p21186_b"])
    assert again.status_code == 409 and again.json() == {"detail": "当前状态 已过账 不可过账"}, again.text
    tb = _trial_balance(client, world, "2026-08")
    assert (tb["total_debit"], tb["total_credit"], tb["balanced"]) == (50000, 50000, True)

    voided = _draft(client, world, "P21186-3", "2026-08")
    assert client.post(f"{A}/vouchers/{voided}/void", headers=world["p21186_b"], json={"reason": "重复录入"}).status_code == 200
    late = client.post(f"{A}/vouchers/{voided}/post", headers=world["p21186_a"])
    assert late.status_code == 409 and late.json() == {"detail": "当前状态 已作废 不可过账"}, late.text
    assert _trial_balance(client, world, "2026-08")["total_debit"] == 50000   # 只有已过账的那一张
