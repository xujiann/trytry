"""医废入暂存与交接是锁外读改写：交接先提交、入暂存后提交，「已交接」被改回「已暂存」（P2-307）。

`store` / `handover` 原先都是「读 status → 判 → 赋值 → commit」，UPDATE 只有 `WHERE id = ?`。暂存间与转运同时扫这一包：
交接先提交、入暂存后提交，转运车上的一包又记回暂存间里，滞留预警跟着报它「超时未交接」；两人同时交接，后写的把先写的
经手人盖掉。

修法：两步都把判定与写入压进同一条 `WHERE status IN (前态)` 的 UPDATE（`_move_waste`），抢输的一路 409、与顺序请求同一句。
这里把「这一路读到之后、写入之前，另一路先提交了」钉成确定的时序：在两处都先调、恰在读取之后的机构归属判定里插进另一路。
"""
import pytest

from app.database import SessionLocal

B = "/api/medwaste"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2307 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    storage = client.post(f"{B}/locations", headers=admin, json={
        "org_id": org, "name": "P2307 暂存间", "location_type": "storage"})
    assert storage.status_code == 201, storage.text
    return {"org": org, "storage": storage.json()["id"]}


def _waste(client, admin, world):
    waste = client.post(B, headers=admin, json={
        "org_id": world["org"], "waste_type": "infectious", "weight_kg": 1.5, "collected_date": "2026-09-26"})
    assert waste.status_code == 201, waste.text
    return waste.json()["id"]


def _row(waste_id):
    from app.models import MedicalWaste

    with SessionLocal() as db:
        waste = db.get(MedicalWaste, waste_id)
        return waste.status, waste.handler_name


def _other_hands_over_first(monkeypatch, waste_id, handler_name):
    from app.models import MedicalWaste
    from app.routers import medwaste

    real = medwaste.assert_obj_org_writable

    def racing(db, user, obj, *args, **kwargs):
        real(db, user, obj, *args, **kwargs)
        with SessionLocal() as other:   # 这一路读到这包医废之后，转运那边先交接完了
            row = other.get(MedicalWaste, waste_id)
            row.status, row.handler_name = "handed_over", handler_name
            other.commit()

    monkeypatch.setattr(medwaste, "assert_obj_org_writable", racing)


def test_交接先提交_入暂存后到_不把已交接改回已暂存(client, admin, world, monkeypatch):
    waste_id = _waste(client, admin, world)
    _other_hands_over_first(monkeypatch, waste_id, "转运员甲")
    got = client.post(f"{B}/{waste_id}/store", headers=admin, json={"storage_location_id": world["storage"]})
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200
    assert got.json()["detail"] == "当前状态 已交接 不可入暂存"   # 与顺序请求同一句，按库里此刻的状态说
    assert _row(waste_id) == ("handed_over", "转运员甲")   # 修前 stored


def test_两人同时交接_后到的409_不盖掉先到的经手人(client, admin, world, monkeypatch):
    waste_id = _waste(client, admin, world)
    _other_hands_over_first(monkeypatch, waste_id, "转运员甲")
    got = client.post(f"{B}/{waste_id}/handover", headers=admin, json={"handler_name": "转运员乙"})
    monkeypatch.undo()
    assert got.status_code == 409 and got.json()["detail"] == "该批医废已交接", got.text   # 修前 200
    assert _row(waste_id) == ("handed_over", "转运员甲")   # 修前经手人被改成乙


def test_不并发时照常_收集_暂存_交接(client, admin, world):
    waste_id = _waste(client, admin, world)
    stored = client.post(f"{B}/{waste_id}/store", headers=admin, json={"storage_location_id": world["storage"]})
    assert stored.status_code == 200 and stored.json()["status"] == "stored", stored.text
    handed = client.post(f"{B}/{waste_id}/handover", headers=admin, json={"handler_name": "转运员丙"})
    assert handed.status_code == 200 and handed.json()["status"] == "handed_over", handed.text
    assert handed.json()["handler_name"] == "转运员丙" and handed.json()["handed_over_at"]
    again = client.post(f"{B}/{waste_id}/store", headers=admin, json={"storage_location_id": world["storage"]})
    assert again.status_code == 409 and again.json()["detail"] == "当前状态 已交接 不可入暂存", again.text
