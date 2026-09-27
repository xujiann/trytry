"""缺药预警只在调拨时秒级广播：发药、批次召回把库存扣到阈值以下，一条都不推（P2-504，第九批「通知承诺」扫描 W2-12）。

系统功能清单写着「WebSocket 危急值 / 缺药预警秒级广播」；`pharmacy.transfer_stock` 调出方低于阈值时定向广播（M-2），
库存最常见的下降途径——发药（`dispense_prescription`）——与批次召回（`recall_batch`）都不推，要等有人打开缺药清单或
待办才看得到。

修法：三处共用一个广播帮手；发药与召回只在「这一笔把库存从阈值上扣到阈值下」时推——库存已经低于阈值之后每发一张方
都推一遍，等于把预警刷成噪音；调拨照旧（调出方低于阈值即推）。
"""
import pytest

CODE = "P2504"


@pytest.fixture
def pushes(monkeypatch):
    from app.ws import manager

    captured: list[dict] = []
    monkeypatch.setattr(manager, "broadcast", lambda message, target_org_id=None: captured.append(message) or True)
    return captured


def _setup(client, admin, name, quantity, threshold):
    org = client.post("/api/organizations", headers=admin, json={
        "name": f"P2504 {name}", "org_type": "township", "level": "township"}).json()["id"]
    batch = client.post("/api/pharmacy/batches", headers=admin, json={
        "org_id": org, "drug_code": CODE, "drug_name": "P2504 片", "batch_no": f"LOT-{name}",
        "expire_date": "2030-12-31", "quantity": quantity})
    assert batch.status_code == 201, batch.text
    stock = client.post("/api/pharmacy/stocks", headers=admin, json={
        "org_id": org, "drug_code": CODE, "drug_name": "P2504 片", "quantity": 0, "threshold": threshold})
    assert stock.status_code == 200 and stock.json()["threshold"] == threshold, stock.text
    return org, batch.json()["id"]


def _dispense(client, admin, org, dose, n):
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2504 患者{n}", "id_card": f"33012719800808{n:03d}X"}).json()["id"]
    rx = client.post("/api/prescriptions", headers=admin, json={
        "patient_id": patient, "org_id": org, "diagnosis_name": "高血压",
        "items": [{"drug_code": CODE, "drug_name": "P2504 片", "daily_dose": dose, "days": 1}]})
    assert rx.status_code == 201, rx.text
    resp = client.post("/api/dispense", headers=admin, json={"prescription_id": rx.json()["id"]})
    assert resp.status_code == 201, resp.text


def _shortages(pushes, org):
    return [(m["quantity"], m["threshold"]) for m in pushes if m.get("type") == "stock_shortage" and m["org_id"] == org]


def test_发药扣到阈值以下推一次_之后不再刷(client, admin, pushes):
    org, _ = _setup(client, admin, "发药院", quantity=100, threshold=50)
    _dispense(client, admin, org, 40, 1)      # 100 → 60，还在阈值上
    assert _shortages(pushes, org) == []
    _dispense(client, admin, org, 20, 2)      # 60 → 40，跨过阈值
    assert _shortages(pushes, org) == [(40, 50)]   # 修前 []
    _dispense(client, admin, org, 10, 3)      # 40 → 30，已在阈值下，不再推
    assert _shortages(pushes, org) == [(40, 50)]


def test_召回把可用余量扣到阈值以下也推(client, admin, pushes):
    org, batch = _setup(client, admin, "召回院", quantity=80, threshold=30)
    resp = client.post(f"/api/pharmacy/batches/{batch}/recall", headers=admin, json={"reason": "P2504 厂家召回"})
    assert resp.status_code == 200, resp.text
    assert _shortages(pushes, org) == [(0, 30)]   # 修前 []


def test_没配阈值不推(client, admin, pushes):
    org, _ = _setup(client, admin, "无阈值院", quantity=20, threshold=0)
    _dispense(client, admin, org, 20, 4)
    assert _shortages(pushes, org) == []
