"""HIS 推送的 ADT^A03 出院只能出本机构的住院（P1-196，第十二批「批量 vs 单条」扫描 Z4-3）。

A01 入院经 create_admission 判病区所属机构，平台出院判住院记录所属机构；A03 原先都不看：乙卫生院的对接账号
按证件号推一条 A03，甲县医院在院的患者就出院了——床位释放、执行中的医嘱停掉、出院随访派出去。
"""
import pytest

from conftest import login

from app.database import SessionLocal
from app.models import Admission

ID_CARD = "330106197706061960"


def _adt(event, control_id, pv1=""):
    lines = [f"MSH|^~\\&|HIS|XZYY|MEDPLAT|COUNTY|20260927090000||{event}|{control_id}|P|2.4",
             f"PID|1||{ID_CARD}^^^CN^ID||P1196 住院患者||19770606|F"]
    if pv1:
        lines.append(pv1)
    return "\r".join(lines)


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {}
    for tag, org_type, level in (("甲", "lead_hospital", "county"), ("乙", "township", "township")):
        orgs[tag] = client.post("/api/organizations", headers=admin, json={
            "name": f"P1196 {tag}院", "org_type": org_type, "level": level}).json()["id"]
    accounts = {}
    for tag in ("甲", "乙"):
        created = client.post("/api/users", headers=admin, json={
            "username": f"p1196_op_{'a' if tag == '甲' else 'b'}", "password": "pass123456", "role": "operator",
            "org_id": orgs[tag]})
        assert created.status_code == 201, created.text
        accounts[tag] = login(client, created.json()["username"], "pass123456")
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": orgs["甲"], "name": "P1196 内科"}).json()
    client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward["id"], "bed_no": "6"})
    resp = client.post("/api/integration/hl7v2/adt", headers=accounts["甲"], json={
        "message": _adt("ADT^A01", "P1196A01", pv1="PV1|1|I|P1196 内科^1^6||||1001^李^主任")})
    assert resp.status_code == 201, resp.text
    return {"accounts": accounts, "admission": resp.json()["admission_id"]}


def _status(admission_id: int) -> str:
    with SessionLocal() as db:
        return db.get(Admission, admission_id).status


def test_别家的对接账号推A03_拒收且患者仍在院(client, admin, world):
    resp = client.post("/api/integration/hl7v2/adt", headers=world["accounts"]["乙"],
                       json={"message": _adt("ADT^A03", "P1196A03B")})
    assert resp.status_code == 403, resp.text   # 修前 201，甲院的患者出院、床位释放
    assert _status(world["admission"]) == "admitted"
    logs = client.get("/api/integration/exchange-logs?message_type=hl7v2_adt_a03", headers=admin).json()
    assert any(not log["success"] and log["error_detail"].startswith("403") for log in logs["logs"])   # 拒收照样留痕


def test_本机构的对接账号推A03_照常出院(client, world):
    resp = client.post("/api/integration/hl7v2/adt", headers=world["accounts"]["甲"],
                       json={"message": _adt("ADT^A03", "P1196A03A")})
    assert resp.status_code == 201, resp.text
    assert _status(world["admission"]) == "discharged"


@pytest.mark.parametrize(
    "module_name,func_name",
    [("app.routers.inpatient", "discharge_admission"), ("app.routers.integration", "_do_hl7v2_adt")],
)
def test_两个出院入口过同一道机构门(module_name, func_name):
    """A03 镜像与平台端点写的是同一行（test_inpatient_order_admission_gate 盯着它们过同一道并发闸门），机构门同理。"""
    import importlib
    import inspect

    code = inspect.getsource(getattr(importlib.import_module(module_name), func_name))
    assert "assert_obj_org_writable(db, user, admission)" in code, f"{func_name} 不判住院记录所属机构"
