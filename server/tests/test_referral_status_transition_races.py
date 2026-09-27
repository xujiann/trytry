"""转诊接诊 / 退回 / 结案不看状态是不是刚被别人改了：已结案的单子被改成已退回（P2-448）。

`PATCH /api/referrals/{id}/status` 原先是「内存里判状态 → 赋值 → commit」，UPDATE 只有 `WHERE id = ?`：同事刚把单子
接诊、结案，这边照页面上还挂着的「待接诊」点退回，照样 200——已结案的单子成了已退回，结案率的分子少一个
（该指标进绩效评分、再进基金分配）；按顺序点同一下是 409。接诊与退回交错同理，两路都 200、库里是后写的那个。

修法同远程会诊（P2-345）：走条件翻转（`concurrency.move_row`，`WHERE status = 判过的那个`），抢输的一路按库里此刻的
状态 409。这里把「判过了、还没写」钉成确定的时序：归属校验之后、写入之前，让另一路先走完并提交。真并发见
`test_referral_status_pg_races.py`。
"""
import pytest

from app.database import SessionLocal

B = "/api/referrals"


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": f"P2448 {name}", "org_type": otype, "level": level}).json()["id"]
        for name, otype, level in (("转出卫生院", "township", "township"), ("接收县医院", "lead_hospital", "county"))]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2448 转诊患者", "id_card": "330106197004042448"}).json()["id"]
    return {"from": orgs[0], "to": orgs[1], "patient": patient}


def _referral(client, admin, world):
    created = client.post(B, headers=admin, json={
        "patient_id": world["patient"], "from_org_id": world["from"], "to_org_id": world["to"],
        "direction": "up", "reason": "P2448 上转"})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _meanwhile(monkeypatch, rid, status):
    """归属校验之后（状态判定、写入之前），另一路把转诊单改成 `status` 并提交。"""
    from app.models import Referral
    from app.routers import referrals

    real, fired = referrals._assert_receiving_org, []

    def racing(*args, **kwargs):
        result = real(*args, **kwargs)
        if not fired:
            fired.append(True)
            with SessionLocal() as other:
                other.get(Referral, rid).status = status
                other.commit()
        return result

    monkeypatch.setattr(referrals, "_assert_receiving_org", racing)
    return fired


def _status(rid):
    from app.models import Referral

    with SessionLocal() as db:
        return db.get(Referral, rid).status


def test_同事刚结案_这边照旧页面点退回_409且仍是已结案(client, admin, world, monkeypatch):
    rid = _referral(client, admin, world)
    fired = _meanwhile(monkeypatch, rid, "completed")
    resp = client.patch(f"{B}/{rid}/status", headers=admin, json={"status": "rejected"})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200
    assert resp.json() == {"detail": "状态不可从 已结案 变更为 已退回"}
    assert _status(rid) == "completed"          # 修前被改成 rejected


def test_接诊与退回交错_后到的409_不盖掉先到的(client, admin, world, monkeypatch):
    rid = _referral(client, admin, world)
    fired = _meanwhile(monkeypatch, rid, "accepted")
    resp = client.patch(f"{B}/{rid}/status", headers=admin, json={"status": "rejected"})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200
    assert resp.json() == {"detail": "状态不可从 已接诊 变更为 已退回"}
    assert _status(rid) == "accepted"


def test_按顺序推进照旧(client, admin, world):
    rid = _referral(client, admin, world)
    accepted = client.patch(f"{B}/{rid}/status", headers=admin, json={"status": "accepted"})
    assert (accepted.status_code, accepted.json()["status"], accepted.json()["status_label"]) == (200, "accepted", "已接诊")
    completed = client.patch(f"{B}/{rid}/status", headers=admin, json={"status": "completed"})
    assert (completed.status_code, completed.json()["status_label"]) == (200, "已结案")
    again = client.patch(f"{B}/{rid}/status", headers=admin, json={"status": "rejected"})
    assert (again.status_code, again.json()) == (409, {"detail": "状态不可从 已结案 变更为 已退回"})
