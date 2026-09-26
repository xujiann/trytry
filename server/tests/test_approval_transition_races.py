"""三处审批是锁外读改写：两位主任一个批准、一个驳回同时到，后提交的把先提交的结论改掉，两路都 200（P2-403）。

物资采购审批（`approve_purchase`）、签合同（`sign_contract`）、特病申报审核（`review_special_disease`）都是「内存里判状态
→ 往对象上赋值 → commit」，UPDATE 只有 `WHERE id = ?`。同一个文件里的验收（`_mark_received`）、姊妹模块的双通道审核
（P2-312）早就是条件 UPDATE。这里把「这一路读到待审批之后、写入之前，另一路先把结论提交了」钉成确定的时序：在两者
之间必经的归属 / 可见性判定里插进另一路的提交。
"""
import pytest

from conftest import login

from app.database import SessionLocal
from app.models import MaterialPurchase, SpecialDiseaseApp


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2403 医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    heads = {}
    for username, role in (("p2403_op", "operator"), ("p2403_dir", "director")):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": role, "org_id": org, "full_name": username})
        assert created.status_code == 201, created.text
        heads[username] = login(client, username, "passw0rd1")
    suppliers = [client.post("/api/pharmacy/suppliers", headers=admin, json={"name": f"P2403 供应商{n}"}).json()["id"]
                 for n in ("甲", "乙")]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2403 患者", "id_card": "330127197309092403"}).json()["id"]
    return {"org": org, "suppliers": suppliers, "patient": patient, **heads}


def _purchase(client, world):
    got = client.post("/api/materials/purchases", headers=world["p2403_op"], json={
        "org_id": world["org"], "item_name": "P2403 输液泵", "spec": "标准", "unit": "台", "quantity": 2,
        "estimated_price": 100, "reason": "P2403"})
    assert got.status_code == 201, got.text
    return got.json()["id"]


def _committed_meanwhile(monkeypatch, module, hook, model, row_id, **values):
    """这一路过了归属 / 可见性判定、还没写：另一路先把结论写进库并提交。"""
    real = getattr(module, hook)

    def racing(*args, **kwargs):
        result = real(*args, **kwargs)
        with SessionLocal() as other:
            row = other.get(model, row_id)
            for key, value in values.items():
                setattr(row, key, value)
            other.commit()
        return result

    monkeypatch.setattr(module, hook, racing)


def _row(model, row_id, *cols):
    with SessionLocal() as db:
        row = db.get(model, row_id)
        return tuple(getattr(row, c) for c in cols)


def test_采购审批与驳回交错_后到的一路409_结论不被改掉(client, world, monkeypatch):
    from app.routers import materials

    pid = _purchase(client, world)
    _committed_meanwhile(monkeypatch, materials, "assert_obj_org_writable", MaterialPurchase, pid, status="cancelled")
    got = client.post(f"/api/materials/purchases/{pid}/approve", headers=world["p2403_dir"], json={"approved": True})
    monkeypatch.undo()
    assert got.status_code == 409 and got.json()["detail"].startswith("当前状态"), got.text   # 修前 200
    assert _row(MaterialPurchase, pid, "status") == ("cancelled",)   # 修前 approved：驳回被改成批准


def test_两路同时签合同_后到的一路409_先签的合同不被盖掉(client, world, monkeypatch):
    from app.routers import materials

    pid = _purchase(client, world)
    assert client.post(f"/api/materials/purchases/{pid}/approve", headers=world["p2403_dir"],
                       json={"approved": True}).status_code == 200
    _committed_meanwhile(monkeypatch, materials, "assert_obj_org_writable", MaterialPurchase, pid,
                         status="contracted", contract_no="HT-OTHER", supplier_id=world["suppliers"][1],
                         contract_amount=999)
    got = client.post(f"/api/materials/purchases/{pid}/contract", headers=world["p2403_op"], json={
        "supplier_id": world["suppliers"][0], "contract_no": "HT-MINE", "contract_amount": 300})
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200
    assert _row(MaterialPurchase, pid, "contract_no", "supplier_id", "contract_amount") == (
        "HT-OTHER", world["suppliers"][1], 999)   # 修前 HT-MINE / 甲 / 300


def test_特病审核批准与驳回交错_后到的一路409_结论不被改掉(client, admin, world, monkeypatch):
    from app.routers import insurance

    app_id = client.post("/api/insurance/special-diseases", headers=world["p2403_op"], json={
        "patient_id": world["patient"], "disease_name": "P2403 尿毒症透析"}).json()["id"]
    _committed_meanwhile(monkeypatch, insurance, "assert_patient_visible", SpecialDiseaseApp, app_id, status="rejected")
    got = client.post(f"/api/insurance/special-diseases/{app_id}/review", headers=world["p2403_dir"],
                      params={"approve": True})
    monkeypatch.undo()
    assert (got.status_code, got.json()["detail"]) == (409, "该申报已处理"), got.text   # 修前 200
    assert _row(SpecialDiseaseApp, app_id, "status") == ("rejected",)   # 修前 approved


def test_不并发时照常审批签约审核(client, world):
    pid = _purchase(client, world)
    assert client.post(f"/api/materials/purchases/{pid}/approve", headers=world["p2403_dir"],
                       json={"approved": True}).json()["status"] == "approved"
    signed = client.post(f"/api/materials/purchases/{pid}/contract", headers=world["p2403_op"], json={
        "supplier_id": world["suppliers"][0], "contract_no": "HT-P2403", "contract_amount": 200})
    assert signed.status_code == 200 and signed.json()["status"] == "contracted", signed.text
    app_id = client.post("/api/insurance/special-diseases", headers=world["p2403_op"], json={
        "patient_id": world["patient"], "disease_name": "P2403 恶性肿瘤放化疗"}).json()["id"]
    reviewed = client.post(f"/api/insurance/special-diseases/{app_id}/review", headers=world["p2403_dir"],
                           params={"approve": True})
    assert reviewed.status_code == 200 and reviewed.json()["status"] == "approved", reviewed.text
