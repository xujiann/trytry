"""登记医嘱执行与停医嘱 / 出院同时到：执行记录落在已停止的医嘱上（P2-1189，第三十四批扫描 L1-12）。

`record_order_execution` 原先只在锁外判医嘱「执行中」、判完直接插执行记录；停医嘱（`stop_order`）改这条医嘱，出院
（`discharge_admission` 与 HL7 A03）在置出院的同一事务里把执行中的医嘱整批停掉。登记执行读到医嘱之后、写入之前，出院或
停医嘱先提交了，执行照样 201：出院 19:27:44.18、执行 19:27:44.20，执行记录挂在已停止的医嘱上；顺序发生时停止之后再登记
是 409。P2-274 把开医嘱挪进住院登记行锁时，注释里写的正是「照样能登记执行」。

修法：判定与插入圈进这条医嘱那一行的临界区（`serialized_on(InpatientOrder)`），锁里按列再判「执行中」，不在执行就回滚、
409，文案与顺序请求同一句。这里把「读到之后、写入之前」钉成确定的时序（同 `test_approval_transition_races.py`）：在两者
之间必经的归属校验（`assert_obj_org_writable`）里，经真实接口插一路出院或停医嘱并提交——登记执行随后在锁外判「执行中」
用的是先前读到的那份医嘱。
"""
import pytest

from app.database import SessionLocal
from app.models import OrderExecution

B = "/api/inpatient"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21189 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post(f"{B}/wards", headers=admin, json={"org_id": org, "name": "P21189 内科病区"}).json()["id"]
    return {"ward": ward, "n": 0}


def _order(client, admin, world):
    """一次在院（病案首页已填、没有费用，随时可出院）上的一条执行中长期医嘱，返回（住院号, 医嘱号）。"""
    world["n"] += 1
    bed = client.post(f"{B}/beds", headers=admin, json={"ward_id": world["ward"], "bed_no": f"P21189-{world['n']}"})
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P21189 患者{world['n']}", "id_card": f"33012719580{world['n']}031189"}).json()["id"]
    admission = client.post(f"{B}/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": world["ward"], "bed_id": bed.json()["id"], "diagnosis_name": "社区获得性肺炎"})
    assert admission.status_code == 201, admission.text
    admission_id = admission.json()["id"]
    summary = client.post(f"{B}/admissions/{admission_id}/case-summary", headers=admin, json={
        "discharge_diagnosis": "社区获得性肺炎"})
    assert summary.status_code == 201, summary.text
    order = client.post(f"{B}/orders", headers=admin, json={
        "admission_id": admission_id, "order_type": "long", "content": "头孢曲松 2g ivgtt qd"})
    assert order.status_code == 201 and order.json()["status"] == "active", order.text
    return admission_id, order.json()["id"]


def _executions(order_id):
    with SessionLocal() as db:
        return db.query(OrderExecution).filter(OrderExecution.inpatient_order_id == order_id).count()


def _execute_while(client, admin, monkeypatch, order_id, path):
    """登记执行过了归属校验、还没写：另一路经真实接口 POST `path` 并提交。返回（登记执行的响应, 插进来那一路的响应）。"""
    from app.routers import inpatient

    real, fired = inpatient.assert_obj_org_writable, []

    def racing(db, user, obj, *args, **kwargs):
        result = real(db, user, obj, *args, **kwargs)
        if not fired:   # 先记上：插进来的出院 / 停医嘱自己也过这道归属校验
            fired.append(path)
            fired.append(client.post(path, headers=admin))
        return result

    monkeypatch.setattr(inpatient, "assert_obj_org_writable", racing)
    got = client.post(f"{B}/orders/{order_id}/executions", headers=admin, json={"note": "已输注"})
    monkeypatch.undo()
    assert fired, "插桩没有触发：登记执行不再在读到医嘱与写入之间做归属校验了，换一个插点"
    return got, fired[1]


def test_登记执行读到医嘱还没写时出院先提交_登记执行409(client, admin, world, monkeypatch):
    admission_id, order_id = _order(client, admin, world)
    got, discharged = _execute_while(client, admin, monkeypatch, order_id, f"{B}/admissions/{admission_id}/discharge")
    assert discharged.status_code == 200 and discharged.json()["status"] == "discharged", discharged.text
    assert got.status_code == 409, got.text   # 修前 201：执行时刻晚于出院、挂在已停止的医嘱上
    assert got.json() == {"detail": "医嘱已停止，不可再登记执行"}   # 与顺序请求同一句
    assert _executions(order_id) == 0


def test_登记执行读到医嘱还没写时停医嘱先提交_登记执行409(client, admin, world, monkeypatch):
    _, order_id = _order(client, admin, world)
    got, stopped = _execute_while(client, admin, monkeypatch, order_id, f"{B}/orders/{order_id}/stop")
    assert stopped.status_code == 200 and stopped.json()["status"] == "stopped", stopped.text
    assert got.status_code == 409, got.text   # 修前 201
    assert got.json() == {"detail": "医嘱已停止，不可再登记执行"}
    assert _executions(order_id) == 0


def test_不并发时照常登记_停止之后409(client, admin, world):
    _, order_id = _order(client, admin, world)
    first = client.post(f"{B}/orders/{order_id}/executions", headers=admin, json={"note": "首剂", "skin_test_result": "negative"})
    assert first.status_code == 201, first.text
    assert (first.json()["inpatient_order_id"], first.json()["note"], first.json()["skin_test_result"]) == (
        order_id, "首剂", "negative")
    assert client.post(f"{B}/orders/{order_id}/stop", headers=admin).status_code == 200
    late = client.post(f"{B}/orders/{order_id}/executions", headers=admin, json={"note": "停止之后"})
    assert late.status_code == 409 and late.json() == {"detail": "医嘱已停止，不可再登记执行"}, late.text
    assert _executions(order_id) == 1
