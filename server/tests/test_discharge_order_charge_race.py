"""出院与开医嘱 / 记住院费用并发：锁外判完「在院」，新医嘱、新费用在出院之后才落库（P2-274）。

`create_order` 与 `create_bill_detail` 都只在锁外判 `admission.status == "admitted"`。出院（`discharge_admission`）在住院
登记这一行上做条件 UPDATE 并停掉全部在执行医嘱；与它并发时两边读到的都还是「在院」：
- 开医嘱：新医嘱在出院停完医嘱之后才落库，以「执行中」挂在已出院的住院上，照样能登记执行——违背同一处写着的
  「患者已出院，不可开立医嘱」；
- 记费：锁里只查结算单（P1-141），可零费用的住院不结算也能出院（`_assert_billing_settled` 只拦未结清的）——一笔费用
  记进已出院的住院，违背「患者已出院，不可继续计费」。

反过来，出院自己在锁外判「费用已结清」，判完到置出院之间若有一笔计费提交，就带着未结清的费用出了院。

修法：两处都在住院登记行的临界区里再判一次「在院」（开医嘱刷新对象，记费直接查这一列）；出院在拿到行锁（置出院的
条件 UPDATE）之后再判一次费用结清，不清就回滚。PG 上出院的条件 UPDATE 与 `FOR UPDATE` 互斥，锁到手时读到的就是
对方提交之后的值（真并发见 `test_discharge_order_races.py`）。这里把「另一路在锁外判定之后、写入之前提交」钉成确定的
时序：开医嘱 / 记费插在归属校验里（恰在判定与写入之间），出院插在置出院那条 UPDATE 之前。
"""
from datetime import datetime

import pytest

from app.database import SessionLocal

B = "/api/inpatient"
ITEM = "P2274-BED"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2274 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post(f"{B}/wards", headers=admin, json={"org_id": org, "name": "P2274 内科病区"}).json()["id"]
    item = client.post("/api/billing/charge-items", headers=admin, json={
        "code": ITEM, "name": "床位费(P2274)", "category": "bed", "price": 60})
    assert item.status_code == 201, item.text
    return {"ward": ward, "n": 0}


def _admission(client, admin, world):
    world["n"] += 1
    bed = client.post(f"{B}/beds", headers=admin, json={"ward_id": world["ward"], "bed_no": f"P2274-{world['n']}"}).json()
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2274 患者{world['n']}", "id_card": f"33012719720{world['n']}152274"}).json()["id"]
    admission = client.post(f"{B}/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": world["ward"], "bed_id": bed["id"], "doctor_name": "内科医生",
        "diagnosis_name": "肺炎"})
    assert admission.status_code == 201, admission.text
    return admission.json()


def _discharge_behind(admission_id):
    """另一路出院提交后的样子：登记置出院、在执行医嘱全停（与 `discharge_admission` 落库的结果相同）。"""
    from app.models import Admission, InpatientOrder

    now = datetime(2026, 9, 26, 9, 0, 0)
    with SessionLocal() as db:
        admission = db.get(Admission, admission_id)
        admission.status, admission.discharged_at = "discharged", now
        db.query(InpatientOrder).filter(
            InpatientOrder.admission_id == admission_id, InpatientOrder.status == "active"
        ).update({InpatientOrder.status: "stopped", InpatientOrder.stopped_at: now}, synchronize_session=False)
        db.commit()


def _race_through(monkeypatch, module, admission_id):
    real = module.assert_obj_org_writable

    def racing(db, user, obj):
        real(db, user, obj)
        _discharge_behind(admission_id)   # 锁外判完「在院」之后、写入之前，出院提交了

    monkeypatch.setattr(module, "assert_obj_org_writable", racing)


def test_开医嘱与出院并发_新医嘱不以执行中挂在已出院的住院上(client, admin, world, monkeypatch):
    from app.models import InpatientOrder
    from app.routers import inpatient

    admission = _admission(client, admin, world)
    _race_through(monkeypatch, inpatient, admission["id"])
    got = client.post(f"{B}/orders", headers=admin, json={
        "admission_id": admission["id"], "order_type": "temp", "content": "P2274 临时医嘱"})
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 201
    assert got.json()["detail"] == "患者已出院，不可开立医嘱"   # 与顺序请求同一句
    with SessionLocal() as db:
        assert db.query(InpatientOrder).filter_by(admission_id=admission["id"]).count() == 0


def test_记住院费用与零费用出院并发_费用不记进已出院的住院(client, admin, world, monkeypatch):
    from app.models import BillDetail
    from app.routers import billing

    admission = _admission(client, admin, world)
    _race_through(monkeypatch, billing, admission["id"])
    got = client.post("/api/billing/details", headers=admin, json={
        "patient_id": admission["patient_id"], "admission_id": admission["id"], "item_code": ITEM, "quantity": 1})
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 201
    assert got.json()["detail"] == "患者已出院，不可继续计费"
    with SessionLocal() as db:
        assert db.query(BillDetail).filter_by(admission_id=admission["id"]).count() == 0


def test_不并发时照常开医嘱与记费(client, admin, world):
    admission = _admission(client, admin, world)
    order = client.post(f"{B}/orders", headers=admin, json={
        "admission_id": admission["id"], "order_type": "long", "content": "P2274 长期医嘱"})
    assert order.status_code == 201, order.text
    again = client.post(f"{B}/orders", headers=admin, json={
        "admission_id": admission["id"], "order_type": "long", "content": "P2274 长期医嘱"})
    assert again.status_code == 409, again.text   # 查重照旧在锁里
    charge = client.post("/api/billing/details", headers=admin, json={
        "patient_id": admission["patient_id"], "admission_id": admission["id"], "item_code": ITEM, "quantity": 2})
    assert charge.status_code == 201, charge.text


def test_出院锁外判完费用结清之后有一笔计费提交_不带着未结清费用出院(client, admin, world, monkeypatch):
    from app.models import Admission, BillDetail, CaseSummary, User
    from app.routers import inpatient

    admission = _admission(client, admin, world)
    with SessionLocal() as db:
        db.add(CaseSummary(admission_id=admission["id"], discharge_diagnosis="肺炎"))
        db.commit()
    real = inpatient._mark_discharged

    def charge_then_mark(db, admission_id, now):
        with SessionLocal() as other:   # 另一路计费在出院锁外判完「已结清」之后提交
            operator = other.query(User).filter_by(username="admin").one()
            other.add(BillDetail(patient_id=admission["patient_id"], admission_id=admission_id, item_code=ITEM,
                                 item_name="床位费(P2274)", unit_price=60, quantity=1, amount=60,
                                 created_by=operator.id))
            other.commit()
        return real(db, admission_id, now)

    monkeypatch.setattr(inpatient, "_mark_discharged", charge_then_mark)
    got = client.post(f"{B}/admissions/{admission['id']}/discharge", headers=admin)
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200：带着 60 元未结清费用出了院
    assert got.json()["detail"] == "存在未结清住院费用 60.00 元，结算后方可出院"   # 与顺序请求同一句
    with SessionLocal() as db:
        assert db.get(Admission, admission["id"]).status == "admitted"   # 置出院那条 UPDATE 随之回滚
