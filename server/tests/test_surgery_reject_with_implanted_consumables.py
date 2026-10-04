"""否决一张已按它登记了高值耗材的手术申请，追溯链记成「植入于已取消的手术」（P2-1399，第四十一批「手术与麻醉闭环」扫描 AE2-6）。

P2-763 的反方向：`use_consumable` 拦「往已取消的手术上登记」，否决却不看这张申请上已登记的耗材。修前实测：急诊申请待审批时
支架登记 200 → 主任否决 200 → 追溯显示 `used 乙患者 手术= 3 冠脉支架植入术`，申请已是已取消；想改挂到重提的申请 → 409
「当前状态 已使用 不可使用」（登记没有逆操作，P2-752）。修法：有耗材的 `used_surgery_id` 指向它时否决 409，文案列出条码
（多的写「等 N 件」），申请仍待审批（照样可以批准）；「没有耗材指向它」与「还待审批」压在同一条 UPDATE 里。
"""
import pytest

from conftest import login

from app.database import SessionLocal
from app.models import HighValueConsumable, SurgeryRequest
from app.routers import surgery

S = "/api/surgery"
M = "/api/materials/consumables"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21399 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    resp = client.post("/api/users", headers=admin, json={
        "username": "p21399_doc", "password": "passw0rd1", "full_name": "P21399 医生", "role": "doctor", "org_id": org})
    assert resp.status_code in (200, 201), resp.text
    doctor = login(client, "p21399_doc", "passw0rd1")
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21399 心内科"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P21399-1"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21399 乙患者", "id_card": "330102197006061399"}).json()["id"]
    admission = client.post("/api/inpatient/admissions", headers=doctor, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "急性心肌梗死"})
    assert admission.status_code == 201, admission.text
    return {"org": org, "doctor": doctor, "patient": patient, "admission": admission.json()["id"]}


def _request(client, world, name):
    """申请由医生提（急诊先做后批），审批 / 否决由管理员来（申请人不得自批）。"""
    resp = client.post(f"{S}/requests", headers=world["doctor"], json={
        "admission_id": world["admission"], "surgery_name": name, "urgency": "emergency"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _implant(client, admin, world, barcode, rid):
    item = client.post(M, headers=admin, json={
        "barcode": barcode, "name": "药物洗脱支架", "org_id": world["org"], "expire_date": "2099-12-31"})
    assert item.status_code == 201, item.text
    used = client.post(f"{M}/{barcode}/use", headers=world["doctor"], json={"patient_id": world["patient"], "surgery_id": rid})
    assert used.status_code == 200, used.text   # 待审批的申请可以先登记耗材（急诊先做后批，P2-763 注释）


def _decide(client, admin, rid, approved):
    return client.post(f"{S}/requests/{rid}/approve", headers=admin, json={"approved": approved})


def _status(rid):
    with SessionLocal() as db:
        return db.get(SurgeryRequest, rid).status


def test_登记了耗材之后否决_409_列出条码_申请仍待审批(client, admin, world):
    rid = _request(client, world, "P21399 冠脉支架植入术")
    _implant(client, admin, world, "P21399-STENT-1", rid)
    resp = _decide(client, admin, rid, False)
    assert resp.status_code == 409, resp.text   # 修前 200：追溯链记成植入于已取消的手术
    assert resp.json()["detail"] == ("这张申请已登记了高值耗材（条码 P21399-STENT-1），"
                                     "不能否决——否决后耗材追溯会记成植入于一台已取消的手术")
    assert _status(rid) == "requested"
    with SessionLocal() as db:
        item = db.query(HighValueConsumable).filter_by(barcode="P21399-STENT-1").one()
        assert (item.status, item.used_surgery_id) == ("used", rid)
    # 申请没被堵死：做过的手术照样批得了
    approved = _decide(client, admin, rid, True)
    assert approved.status_code == 200 and approved.json()["status"] == "approved", approved.text


def test_耗材多的只列前几个条码_写明共几件(client, admin, world):
    rid = _request(client, world, "P21399 多支架植入术")
    codes = [f"P21399-MULTI-{n}" for n in range(1, surgery.IMPLANT_BARCODES_SHOWN + 3)]
    for code in codes:
        _implant(client, admin, world, code, rid)
    resp = _decide(client, admin, rid, False)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    shown = "、".join(codes[:surgery.IMPLANT_BARCODES_SHOWN])
    assert f"（条码 {shown} 等 {len(codes)} 件）" in detail, detail
    assert codes[-1] not in detail
    assert _status(rid) == "requested"


def test_没有耗材的照常否决(client, admin, world):
    rid = _request(client, world, "P21399 改保守治疗")
    resp = _decide(client, admin, rid, False)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"


def test_锁外那一眼之后才登记上的_同一条UPDATE照样拦下(client, admin, world, monkeypatch):
    """预检是锁外读的：让第一次预检看不见（等于登记恰在它之后、翻转之前提交），「没有耗材指向它」压在那条带状态条件的
    UPDATE 里照样拦下，报的仍是耗材那一句、不是「当前状态 待审批 不可审批」。"""
    rid = _request(client, world, "P21399 抢先登记")
    _implant(client, admin, world, "P21399-RACE-1", rid)
    real, calls = surgery._refuse_if_implanted, []

    def blind_first(db, request_id):
        calls.append(request_id)
        if len(calls) > 1:
            real(db, request_id)

    monkeypatch.setattr(surgery, "_refuse_if_implanted", blind_first)
    resp = _decide(client, admin, rid, False)
    assert resp.status_code == 409, resp.text
    assert "条码 P21399-RACE-1" in resp.json()["detail"]
    assert calls == [rid, rid]
    assert _status(rid) == "requested"
