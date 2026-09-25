"""高值耗材使用登记不是原子的：同一条码并发登记给两位患者，都 200、追溯链记成后写的那位（P2-113）。

`use_consumable` 原先「内存里判 in_stock → 改 used、记患者与手术」。高值耗材一物一码（支架、人工晶体……），
PG 的 READ COMMITTED 下同一条码的两次并发登记都读到在库、都往下走：两路都 200，库里只留后写的那位——
一枚耗材只植入了一个人，追溯链却可能记在另一个人身上，按批号召回时找错人，另一边的登记人还以为自己登记上了。

修法同采购验收（`_mark_received`）：状态翻转与使用信息压进同一条带状态条件的 UPDATE（`WHERE status = 'in_stock'`），
抢输的一路 409，与顺序重复登记同一句。

这里用「一路拿着先读到的对象、另一路先提交」把并发窗口钉成确定的时序（SQLite 与 PG 同样成立）；
PG 上真并发的不变量见 test_consumable_use_transition_races.py。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2113 介入中心", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patients = [
        client.post("/api/patients", headers=admin, json={
            "name": f"P2113 患者{i}", "id_card": f"33012719720{i}132113"}).json()["id"]
        for i in range(2)
    ]
    item = client.post("/api/materials/consumables", headers=admin, json={
        "barcode": "P2113-STENT-0001", "name": "冠脉支架", "org_id": org, "expire_date": "2099-12-31"})
    assert item.status_code == 201, item.text
    return {"patients": patients, "barcode": "P2113-STENT-0001"}


def _use(db, barcode, patient_id):
    from app.models import User
    from app.routers.materials import UseIn, use_consumable

    return use_consumable(barcode, UseIn(patient_id=patient_id), db=db,
                          user=db.query(User).filter_by(username="admin").one())


def test_同一条码并发登记给两位患者_只成一路_追溯链不被改写(world):
    from fastapi import HTTPException

    from app.database import SessionLocal
    from app.models import HighValueConsumable

    first, second = world["patients"]
    with SessionLocal() as racer, SessionLocal() as winner:
        held = racer.query(HighValueConsumable).filter_by(barcode=world["barcode"]).one()   # noqa: F841 — 第二路先读到在库
        _use(winner, world["barcode"], first)                  # 第一路先登记给患者甲
        with pytest.raises(HTTPException) as exc:
            _use(racer, world["barcode"], second)               # 第二路拿着读到的在库接着登记给患者乙
    assert exc.value.status_code == 409   # 修前不报错
    assert exc.value.detail == "当前状态 已使用 不可使用"   # 与顺序重复登记同一句
    with SessionLocal() as db:
        item = db.query(HighValueConsumable).filter_by(barcode=world["barcode"]).one()
        assert (item.status, item.used_patient_id) == ("used", first)   # 修前记成了患者乙
