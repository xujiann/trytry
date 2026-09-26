"""医废交接只填员工号必 422：姓名继承了必填，交接弹窗的主路「员工ID（优先，姓名由档案带出）」走不通（P2-305）。

交接弹窗两栏：「转运人员工ID（优先，姓名由档案带出）」「转运人姓名（没有员工档案时填）」，只填员工号时送
`{handler_name: "", handler_employee_id: …}`；`WasteHandoverIn` 继承 `WasteHandover`，姓名是 `min_length=1` 的必填——
这条主路一律 422，经办只好把姓名再抄一遍（而后端又不采信它，名字取自档案）。

修法：员工号与姓名二选一——填了员工号的姓名可留空（照旧取档案里的），没填员工号的姓名仍须非空白；两样都没有 422。
"""
import pytest

B = "/api/medwaste"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2305 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    employee = client.post("/api/mgmt/employees", headers=admin, json={"org_id": org, "name": "P2305 转运员"})
    assert employee.status_code == 201, employee.text
    return {"org": org, "employee": employee.json()["id"]}


def _waste(client, admin, world):
    waste = client.post(B, headers=admin, json={
        "org_id": world["org"], "waste_type": "infectious", "weight_kg": 2.0, "collected_date": "2026-09-26"})
    assert waste.status_code == 201, waste.text
    return waste.json()["id"]


def test_只填员工号照收_姓名取档案(client, admin, world):
    got = client.post(f"{B}/{_waste(client, admin, world)}/handover", headers=admin,
                      json={"handler_name": "", "handler_employee_id": world["employee"]})   # 弹窗只填员工号时送的就是这个
    assert got.status_code == 200, got.text   # 修前 422
    assert (got.json()["handler_name"], got.json()["handler_employee_id"]) == ("P2305 转运员", world["employee"])


def test_只填姓名照旧_两样都没有或姓名全是空格422(client, admin, world):
    ok = client.post(f"{B}/{_waste(client, admin, world)}/handover", headers=admin, json={"handler_name": "外包转运员"})
    assert ok.status_code == 200 and ok.json()["handler_name"] == "外包转运员", ok.text
    for body in ({"handler_name": ""}, {"handler_name": "   "}, {}):
        got = client.post(f"{B}/{_waste(client, admin, world)}/handover", headers=admin, json=body)
        assert got.status_code == 422, (body, got.text)
