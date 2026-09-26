"""远程会诊的受理 / 拒绝 / 出具意见不看状态是不是刚被别人改了：先写的意见被整段盖掉（P2-345）。

三个流转端点都是「内存里判状态 → 赋值 → commit」，UPDATE 只有 `WHERE id = ?`。两位专家同时出具意见都 200，先写的意见被
后写的盖掉、申请方读到的只剩一份；同时受理，受理专家记成后写的那位；拒绝与受理交错，已受理的单子被改成已拒绝。

修法：三处走条件翻转（`concurrency.move_row`，`WHERE status = 判过的那个`），抢输的一路按库里此刻的状态 409。
这里把「判过了、还没写」钉成确定的时序：归属校验之后、写入之前，让另一路先走完这一步并提交。
"""
import pytest

from app.database import SessionLocal

B = "/api/consultations"


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": f"P2345 {name}", "org_type": otype, "level": level}).json()["id"]
        for name, otype, level in (("申请卫生院", "township", "township"), ("受邀县医院", "lead_hospital", "county"))]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2345 会诊患者", "id_card": "330106197003032345"}).json()["id"]
    return {"from": orgs[0], "to": orgs[1], "patient": patient}


def _consultation(client, admin, world, status="applied"):
    created = client.post(B, headers=admin, json={
        "patient_id": world["patient"], "from_org_id": world["from"], "to_org_id": world["to"], "question": "P2345 会诊"})
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    if status == "accepted":
        assert client.post(f"{B}/{cid}/accept", headers=admin, json={"expert_name": "专家甲"}).status_code == 200
    return cid


def _meanwhile(monkeypatch, cid, **values):
    """归属校验之后（状态判定、写入之前），另一路把会诊单改成 `values` 并提交。"""
    from app.models import Consultation
    from app.routers import consultations

    real, fired = consultations.assert_patient_visible, []

    def racing(*args, **kwargs):
        result = real(*args, **kwargs)
        if not fired:
            fired.append(True)
            with SessionLocal() as other:
                row = other.get(Consultation, cid)
                for key, value in values.items():
                    setattr(row, key, value)
                other.commit()
        return result

    monkeypatch.setattr(consultations, "assert_patient_visible", racing)
    return fired


def _row(cid):
    from app.models import Consultation

    with SessionLocal() as db:
        row = db.get(Consultation, cid)
        return row.status, row.expert_name, row.opinion


def test_两位专家同时出具意见_后到的409_先写的意见不被盖掉(client, admin, world, monkeypatch):
    cid = _consultation(client, admin, world, status="accepted")
    fired = _meanwhile(monkeypatch, cid, status="completed", opinion="专家乙：建议上转")
    resp = client.post(f"{B}/{cid}/complete", headers=admin, json={"opinion": "专家甲：继续观察"})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200
    assert resp.json() == {"detail": "当前状态 已完成 不可出具意见"}
    assert _row(cid)[2] == "专家乙：建议上转"     # 修前被「专家甲：继续观察」盖掉


def test_同时受理_受理专家不被后写的盖掉(client, admin, world, monkeypatch):
    cid = _consultation(client, admin, world)
    fired = _meanwhile(monkeypatch, cid, status="accepted", expert_name="专家乙")
    resp = client.post(f"{B}/{cid}/accept", headers=admin, json={"expert_name": "专家甲"})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text
    assert resp.json() == {"detail": "当前状态 已受理 不可受理"}
    assert _row(cid)[:2] == ("accepted", "专家乙")


def test_拒绝与受理交错_已受理的不被改成已拒绝(client, admin, world, monkeypatch):
    cid = _consultation(client, admin, world)
    fired = _meanwhile(monkeypatch, cid, status="accepted", expert_name="专家乙")
    resp = client.post(f"{B}/{cid}/decline", headers=admin)
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text
    assert resp.json() == {"detail": "当前状态 已受理 不可拒绝"}
    assert _row(cid)[0] == "accepted"            # 修前 declined


def test_没有竞争时照常流转(client, admin, world):
    cid = _consultation(client, admin, world)
    accepted = client.post(f"{B}/{cid}/accept", headers=admin, json={"expert_name": "专家丙"})
    assert accepted.status_code == 200 and accepted.json()["expert_name"] == "专家丙", accepted.text
    done = client.post(f"{B}/{cid}/complete", headers=admin, json={"opinion": "专家丙：已会诊"})
    assert done.status_code == 200 and done.json()["status"] == "completed", done.text
    assert done.json()["opinion"] == "专家丙：已会诊"
