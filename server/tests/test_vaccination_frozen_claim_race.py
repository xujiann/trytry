"""接种登记的「批次已封存」是锁外读的一眼：读完到扣库存之间被封存（超温、召回），照样扣一支、照样登记接种（P2-235）。

`vaccinate` 先 `db.get` 批次看 `status == "frozen"`，再用 `claim_quota` 一条 UPDATE 扣库存——扣库存那条只看余量，
不看封存。冷链超温报警一响，防保科当场封存这批，同一刻另一个接种台正在登记：封存判断读到的还是「正常」，
扣库存照扣，这一针照样登记在已封存的批次上。库存扣减早就压进同一条 SQL（原注释：实测库存 1 支打出 4 针），
封存这个前提却留在了锁外。

修法：封存也压进占额那条 UPDATE（`claim_quota(..., where=(status != 'frozen',))`），没占到时重读批次，按实际原因
回 409。这里用「占额之前另一个会话先封存并提交」把并发窗口钉成确定的时序（SQLite 与 PG 同样成立）。
"""

import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2235 接种门诊", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2235 受种者", "id_card": "330127201901012235"}).json()["id"]
    batch = client.post("/api/vaccine-supply/batches", headers=admin, json={
        "vaccine_code": "P2235-HEPB", "vaccine_name": "P2235 乙肝疫苗", "batch_no": "P2235-B1",
        "expire_date": "2099-12-31", "org_id": org, "quantity": 5})
    assert batch.status_code == 201, batch.text
    return {"org": org, "patient": patient, "batch": batch.json()["id"]}


def _vaccinate(client, admin, world):
    return client.post("/api/vaccination/records", headers=admin, json={
        "patient_id": world["patient"], "vaccine_code": "P2235-HEPB", "vaccine_name": "P2235 乙肝疫苗",
        "org_id": world["org"], "batch_id": world["batch"]})


def test_看过封存状态之后被封存_这一针不登记_库存不扣(client, admin, world, monkeypatch):
    from app.database import SessionLocal
    from app.models import VaccineBatch
    from app.routers import vaccination

    real_claim = vaccination.claim_quota

    def frozen_meanwhile(db, *args, **kwargs):
        with SessionLocal() as other:   # 读完封存状态、扣库存之前，防保科在另一处封存了这批并提交
            batch = other.get(VaccineBatch, world["batch"])
            batch.status, batch.frozen_reason = "frozen", "冷链超温"
            other.commit()
        return real_claim(db, *args, **kwargs)

    monkeypatch.setattr(vaccination, "claim_quota", frozen_meanwhile)
    resp = _vaccinate(client, admin, world)
    assert resp.status_code == 409, resp.text            # 修前 201：这一针登记在了已封存的批次上
    assert resp.json() == {"detail": "该批次已封存：冷链超温"}
    with SessionLocal() as db:
        assert db.get(VaccineBatch, world["batch"]).used_quantity == 0   # 修前 1
    records = client.get(f"/api/vaccination/records?patient_id={world['patient']}", headers=admin).json()
    assert [r for r in records if r["vaccine_code"] == "P2235-HEPB"] == []


def test_解封之后照常登记_库存用完照旧说库存(client, admin, world):
    from app.database import SessionLocal
    from app.models import VaccineBatch

    assert client.post(f"/api/vaccine-supply/batches/{world['batch']}/unfreeze", headers=admin).status_code == 200
    assert _vaccinate(client, admin, world).status_code == 201
    with SessionLocal() as db:
        batch = db.get(VaccineBatch, world["batch"])
        batch.used_quantity = batch.quantity
        db.commit()
    resp = _vaccinate(client, admin, world)
    assert resp.status_code == 409 and resp.json() == {"detail": "该批次库存已用完"}, resp.text
