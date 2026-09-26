"""冷链超温处置可重复登记、后一次覆盖前一次：先到的处置说明被一句「已处理」盖掉（P2-308）。

`handle_exceedance` 只挡「未超温」，已处置的照收，`handle_note` 整段换成后一次的——先到的那条「已转移至备用冰箱、
报废 3 支」被盖掉，追溯时查不回当时做了什么；两人同时处置同理（锁外读到「未处置」都往下走）。

修法：处置只登记一次，判定与写入压进同一条 `WHERE handled IS false` 的 UPDATE，已处置的 409、处置说明不动。
"""
import pytest

from app.database import SessionLocal

B = "/api/vaccine-supply"


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P2308 卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _hot(client, admin, org, n):
    hot = client.post(f"{B}/cold-chain", headers=admin, json={
        "org_id": org, "device_name": f"P2308 冰箱{n}", "temperature": 12.0, "recorded_at": "2026-09-26 09:00:00"})
    assert hot.status_code == 201 and hot.json()["exceeded"] is True, hot.text
    return hot.json()["id"]


def _note(record_id):
    from app.models import ColdChainRecord

    with SessionLocal() as db:
        record = db.get(ColdChainRecord, record_id)
        return record.handled, record.handle_note


def test_已处置的再登记409_先到的处置说明不被盖掉(client, admin, org):
    record_id = _hot(client, admin, org, 1)
    first = client.post(f"{B}/cold-chain/{record_id}/handle", headers=admin, json={"handle_note": "已转移至备用冰箱、报废 3 支"})
    assert first.status_code == 200 and first.json()["handled"] is True, first.text
    again = client.post(f"{B}/cold-chain/{record_id}/handle", headers=admin, json={"handle_note": "已处理"})
    assert again.status_code == 409, again.text   # 修前 200
    assert again.json()["detail"] == "该超温记录已处置，处置说明不能改写"
    assert _note(record_id) == (True, "已转移至备用冰箱、报废 3 支")   # 修前「已处理」


def test_两人同时处置_只成一路(client, admin, org, monkeypatch):
    from app.models import ColdChainRecord
    from app.routers import vaccine_supply

    record_id = _hot(client, admin, org, 2)
    real = vaccine_supply.assert_obj_org_writable

    def racing(db, user, obj, *args, **kwargs):
        real(db, user, obj, *args, **kwargs)
        with SessionLocal() as other:   # 这一路读到「未处置」之后，另一位先处置完了
            row = other.get(ColdChainRecord, record_id)
            row.handled, row.handle_note = True, "另一位：已转移疫苗"
            other.commit()

    monkeypatch.setattr(vaccine_supply, "assert_obj_org_writable", racing)
    got = client.post(f"{B}/cold-chain/{record_id}/handle", headers=admin, json={"handle_note": "这一路：已处理"})
    monkeypatch.undo()
    assert got.status_code == 409, got.text
    assert _note(record_id) == (True, "另一位：已转移疫苗")
