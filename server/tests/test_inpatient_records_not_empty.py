"""住院护理记录 / 体温单收空记录，一条空记录就消掉文书完整性检查的「缺护理记录」「缺体征记录」（P2-468）。

- 护理记录：`content` 默认空串、没有非空约束，`{}` 照样 201；门诊护理记录同一张表早就要求内容非空。
- 体征：八个测量值全可空（「一次测量未必测全」），只有测量时刻的一条也 201、各值全空，体温单上多一个空点。
- 文书完整性检查（出院前自查、终末质控的抓手）只数行数，两条空记录就让它报「完整」。

修法：护理记录要有内容，执行医嘱产生的可只关联所执行的医嘱；体征至少一项测量值。
"""
import pytest

from test_clinical_surgery import _new_admission


@pytest.fixture(scope="module")
def ward(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2468 县医院", "org_type": "lead_hospital", "level": "county"}).json()
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org["id"], "name": "P2468 内科病区"})
    return {"org": org, "ward": ward.json()}


def test_空护理记录与空体征422_完整性检查不被空记录消掉(client, admin, ward):
    adm = _new_admission(client, admin, ward, "P2468 空记录患者", "331182199104014681")
    base = f"/api/inpatient/admissions/{adm['id']}"
    for body in ({}, {"content": "   ", "nursing_level": "level1"}):
        resp = client.post(f"{base}/nursing-records", headers=admin, json=body)
        assert resp.status_code == 422, resp.text   # 修前 201，内容为空
        assert resp.json() == {"detail": "护理记录要写内容（执行医嘱产生的可只关联所执行的医嘱）"}
    resp = client.post(f"{base}/vitals", headers=admin, json={"measured_at": "2026-09-27 08:00", "recorder": "护士甲"})
    assert resp.status_code == 422, resp.text   # 修前 201，各值全空
    assert resp.json() == {"detail": "一次体征记录至少要有一项测量值"}
    missing = client.get(f"{base}/document-completeness", headers=admin).json()["missing"]
    assert "缺护理记录" in missing and "缺体征记录" in missing   # 修前两条空记录把这两项消掉


def test_只测一项的体征与有内容的护理记录照收(client, admin, ward):
    adm = _new_admission(client, admin, ward, "P2468 照收患者", "331182199104024682")
    base = f"/api/inpatient/admissions/{adm['id']}"
    assert client.post(f"{base}/nursing-records", headers=admin, json={"content": "一级护理，卧床"}).status_code == 201
    for value in ({"weight_kg": 61.5}, {"intake_ml": 800}, {"temperature": 37.1}):
        resp = client.post(f"{base}/vitals", headers=admin, json={"measured_at": "2026-09-27 08:00", **value})
        assert resp.status_code == 201, resp.text


def test_执行医嘱产生的护理记录可只关联医嘱(client, admin, ward):
    adm = _new_admission(client, admin, ward, "P2468 医嘱患者", "331182199104034683")
    order = client.post("/api/inpatient/orders", headers=admin, json={
        "admission_id": adm["id"], "order_type": "long", "content": "0.9% 氯化钠 250ml 静滴 qd"})
    assert order.status_code == 201, order.text
    resp = client.post(f"/api/inpatient/admissions/{adm['id']}/nursing-records", headers=admin,
                       json={"inpatient_order_id": order.json()["id"]})
    assert resp.status_code == 201, resp.text


def test_测量值清单取自模型_新加的测量项自动算数():
    from app.routers.clinical_docs import VITAL_VALUES, VitalIn

    assert set(VITAL_VALUES) == set(VitalIn.model_fields) - {"measured_at", "recorder"}
    assert {"temperature", "weight_kg", "intake_ml", "output_ml"} <= set(VITAL_VALUES)
