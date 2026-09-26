"""退药冲销与批次召回并发：读到「批次正常」之后别人刚召回，退回的量照样加回可用汇总——召回的批次上又长出可发余量（P2-402）。

`reverse_dispense` 的第 2 条口径：「退回不可发批次的量不回可用汇总」。可回补去向是按锁外取到的批次状态判的：召回
提交在「取批次」与「回补批次已用」之间（真 PG 上：召回持着行锁，退药读到的是提交前的「正常」，回补批次已用时等锁、
召回提交后照做），召回早把余量整笔转进不可发，这一笔再按「正常」加回汇总——账上多出一笔一片也发不出的库存，发药
409「批次已召回」，缺药预警与采购建议却当有货。对账不变式照样平（两边同改），既有不变式用例看不出来。

修法：回补批次已用那条 UPDATE 拿到行锁之后重读批次，按重读到的状态定去向。这里把「读到的是召回前的状态」钉成确定的
时序：批次召回之后，让冲销取到的那份对象仍是「正常」（不写库，只改会话里读到的值）。
"""
from sqlalchemy.orm.attributes import set_committed_value

from app.database import SessionLocal
from app.models import DrugBatch, DrugStock

CODE = "P2402"


def _stale_normal(monkeypatch, batch_id):
    from app.routers import dispense

    real, fired = dispense.ensure_present, []

    def stale(obj, *args, **kwargs):
        result = real(obj, *args, **kwargs)
        if isinstance(result, DrugBatch) and result.id == batch_id:
            set_committed_value(result, "status", "normal")
            fired.append(True)
        return result

    monkeypatch.setattr(dispense, "ensure_present", stale)
    return fired


def _state(org_id, batch_id):
    with SessionLocal() as db:
        batch = db.get(DrugBatch, batch_id)
        stock = db.query(DrugStock).filter_by(org_id=org_id, drug_code=CODE).one()
        return stock.quantity, batch.quantity - batch.used_quantity - batch.blocked_quantity, batch.blocked_quantity


def test_冲销时读到正常之后批次被召回_退回的量不回可用汇总(client, admin, monkeypatch):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2402 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2402 患者", "id_card": "330127197309092402"}).json()["id"]
    batch = client.post("/api/pharmacy/batches", headers=admin, json={
        "org_id": org, "drug_code": CODE, "drug_name": "P2402 片", "batch_no": "LOT-1",
        "expire_date": "2030-12-31", "quantity": 100}).json()["id"]
    rx = client.post("/api/prescriptions", headers=admin, json={
        "patient_id": patient, "org_id": org, "diagnosis_name": "上呼吸道感染",
        "items": [{"drug_code": CODE, "drug_name": "P2402 片", "daily_dose": 30, "days": 1}]})
    assert rx.status_code == 201, rx.text
    dispensed = client.post("/api/dispense", headers=admin, json={"prescription_id": rx.json()["id"]})
    assert dispensed.status_code == 201, dispensed.text
    recalled = client.post(f"/api/pharmacy/batches/{batch}/recall", headers=admin, json={"reason": "P2402 厂家召回"})
    assert recalled.status_code == 200, recalled.text
    assert _state(org, batch) == (0, 0, 70)

    fired = _stale_normal(monkeypatch, batch)
    reversed_ = client.post(f"/api/dispense/{dispensed.json()['id']}/reverse", headers=admin, json={"reason": "P2402 退药"})
    monkeypatch.undo()
    assert fired
    assert reversed_.status_code == 200, reversed_.text
    # 修前 (30, 30, 70)：退回的 30 片进了可用汇总，召回的批次上又有 30 片「可发」
    assert _state(org, batch) == (0, 0, 100)


def test_没有召回时冲销照常回可用汇总(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2402 乡镇院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2402 患者乙", "id_card": "330127197309092412"}).json()["id"]
    batch = client.post("/api/pharmacy/batches", headers=admin, json={
        "org_id": org, "drug_code": CODE, "drug_name": "P2402 片", "batch_no": "LOT-2",
        "expire_date": "2030-12-31", "quantity": 50}).json()["id"]
    rx = client.post("/api/prescriptions", headers=admin, json={
        "patient_id": patient, "org_id": org, "diagnosis_name": "上呼吸道感染",
        "items": [{"drug_code": CODE, "drug_name": "P2402 片", "daily_dose": 20, "days": 1}]}).json()["id"]
    dispensed = client.post("/api/dispense", headers=admin, json={"prescription_id": rx}).json()["id"]
    assert client.post(f"/api/dispense/{dispensed}/reverse", headers=admin, json={"reason": "退药"}).status_code == 200
    assert _state(org, batch) == (50, 50, 0)
