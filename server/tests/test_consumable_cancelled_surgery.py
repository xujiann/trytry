"""高值耗材使用登记不能挂到已取消（审批不通过）的手术上（P2-763，第二十批「停用 / 注销 / 作废的对象仍在被用」扫描 M3-9）。

`use_consumable` 原先只查手术存在、患者一致（「耗材记到别人的手术上，追溯链就断了，这里必须拦」），不看手术状态；界面上
「关联手术申请ID」是手填数字，填成被驳回的那张旧申请号也照收——追溯链记成「植入于一台没做的手术」，真正做的那台手术下
查不到这枚支架，登错了也改不回（P2-752）。
"""
import pytest

from conftest import login

from app.database import SessionLocal
from app.models import HighValueConsumable

M = "/api/materials/consumables"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2763 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    resp = client.post("/api/users", headers=admin, json={
        "username": "p2763_doc", "password": "passw0rd1", "full_name": "P2763 李医生", "role": "doctor", "org_id": org})
    assert resp.status_code in (200, 201), resp.text
    doctor = login(client, "p2763_doc", "passw0rd1")
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2763 外科"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "01"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2763 陈先生", "id_card": "330102197001012763"}).json()["id"]
    admission = client.post("/api/inpatient/admissions", headers=doctor, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "冠心病"})
    assert admission.status_code == 201, admission.text
    surgeries = {}
    for key, approved in (("cancelled", False), ("approved", True)):
        request = client.post("/api/surgery/requests", headers=doctor, json={
            "admission_id": admission.json()["id"], "surgery_name": "冠脉支架植入"})
        assert request.status_code == 201, request.text
        decided = client.post(f"/api/surgery/requests/{request.json()['id']}/approve", headers=admin,
                              json={"approved": approved, "note": "改保守治疗" if not approved else "同意"})
        assert decided.status_code == 200 and decided.json()["status"] == key, decided.text
        surgeries[key] = request.json()["id"]
    for barcode in ("P2763-STENT-1", "P2763-STENT-2"):
        item = client.post(M, headers=admin, json={
            "barcode": barcode, "name": "药物洗脱支架", "org_id": org, "expire_date": "2099-12-31"})
        assert item.status_code == 201, item.text
    return {"doctor": doctor, "patient": patient, **surgeries}


def test_登记在已取消的手术上_409_耗材仍在库(client, world):
    resp = client.post(f"{M}/P2763-STENT-1/use", headers=world["doctor"], json={
        "patient_id": world["patient"], "surgery_id": world["cancelled"]})
    assert resp.status_code == 409, resp.text   # 修前 200：追溯链记成植入于一台没做的手术
    assert resp.json()["detail"] == "该手术已取消，不能登记耗材使用"
    with SessionLocal() as db:
        item = db.query(HighValueConsumable).filter_by(barcode="P2763-STENT-1").one()
        assert (item.status, item.used_patient_id, item.used_surgery_id) == ("in_stock", None, None)


def test_登记在已审批的手术上照常(client, world):
    resp = client.post(f"{M}/P2763-STENT-2/use", headers=world["doctor"], json={
        "patient_id": world["patient"], "surgery_id": world["approved"]})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["status"], resp.json()["used_surgery_id"]) == ("used", world["approved"])
