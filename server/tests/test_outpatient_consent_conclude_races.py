"""知情同意书签署 / 拒签不看状态是不是刚被别人改了：已签的告知书被改成拒签、签署时间被改写（P2-449）。

`_pending` 判完「待签署」、赋值、commit，UPDATE 只有 `WHERE id = ?`：张三刚在一台电脑上签完，另一台电脑照旧页面
点「拒签」照样 200——告知书成了李四拒签，`signed_at` 被改写；按顺序点同一下是 409。告知书是证据性文书，docstring
与手册都说「已有结论的不可改写」，这一条要在写入那一刻成立。

修法：签署 / 拒签走条件翻转（`concurrency.move_row`，`WHERE status = 'pending'`），抢输的一路按库里此刻的结论 409。
这里把「判过了、还没写」钉成确定的时序：归属校验之后、写入之前，让另一路先签完并提交。真并发见
`test_outpatient_consent_conclude_pg_races.py`。顺带：409 原先拼成「该告知书已已签署」（状态文案自带「已」）。
"""
import datetime

import pytest

from app.database import SessionLocal

B = "/api/outpatient/consents"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2449 门诊部", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2449 签署患者", "id_card": "330106197005052449"}).json()["id"]
    return {"org": org, "patient": patient}


def _consent(client, admin, world):
    created = client.post(B, headers=admin, json={
        "patient_id": world["patient"], "org_id": world["org"], "consent_type": "surgery",
        "title": "P2449 手术知情同意书", "content": "手术风险告知正文"})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _meanwhile(monkeypatch, cid, **values):
    """归属校验之后（状态判定、写入之前），另一台电脑把告知书改成 `values` 并提交。"""
    from app.models import InformedConsent
    from app.routers import outpatient_docs

    real, fired = outpatient_docs.assert_obj_org_writable, []

    def racing(*args, **kwargs):
        result = real(*args, **kwargs)
        if not fired:
            fired.append(True)
            with SessionLocal() as other:
                row = other.get(InformedConsent, cid)
                for key, value in values.items():
                    setattr(row, key, value)
                other.commit()
        return result

    monkeypatch.setattr(outpatient_docs, "assert_obj_org_writable", racing)
    return fired


def _row(cid):
    from app.models import InformedConsent

    with SessionLocal() as db:
        row = db.get(InformedConsent, cid)
        return row.status, row.signer_name, row.refuse_reason, row.signed_at


SIGNED_AT = datetime.datetime(2026, 9, 27, 1, 0)


def test_张三刚签完_另一台电脑点拒签_409且仍是张三签的(client, admin, world, monkeypatch):
    cid = _consent(client, admin, world)
    fired = _meanwhile(monkeypatch, cid, status="signed", signer_name="张三", signer_relation="self", signed_at=SIGNED_AT)
    resp = client.post(f"{B}/{cid}/refuse", headers=admin, json={
        "signer_name": "李四", "signer_relation": "spouse", "refuse_reason": "不同意手术"})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200
    assert resp.json() == {"detail": "该告知书当前为「已签署」，不可重复处理"}
    assert _row(cid) == ("signed", "张三", "", SIGNED_AT)   # 修前：refused / 李四 / 不同意手术 / 被改写的时间


def test_刚记了拒签_另一台电脑点签署_409且拒签理由还在(client, admin, world, monkeypatch):
    cid = _consent(client, admin, world)
    fired = _meanwhile(monkeypatch, cid, status="refused", signer_name="王五", refuse_reason="要再商量", signed_at=SIGNED_AT)
    resp = client.post(f"{B}/{cid}/sign", headers=admin, json={"signer_name": "赵六"})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200
    assert resp.json() == {"detail": "该告知书当前为「拒绝签署」，不可重复处理"}
    assert _row(cid) == ("refused", "王五", "要再商量", SIGNED_AT)


def test_按顺序签署照旧_再点一次409(client, admin, world):
    cid = _consent(client, admin, world)
    signed = client.post(f"{B}/{cid}/sign", headers=admin, json={"signer_name": "孙七", "signer_relation": "parent"})
    assert signed.status_code == 200, signed.text
    body = signed.json()
    assert (body["status"], body["status_name"], body["signer_name"], body["signer_relation_name"]) == \
        ("signed", "已签署", "孙七", "父母")
    assert body["signed_at"]
    again = client.post(f"{B}/{cid}/refuse", headers=admin, json={"signer_name": "孙七", "refuse_reason": "反悔"})
    assert (again.status_code, again.json()) == (409, {"detail": "该告知书当前为「已签署」，不可重复处理"})
