"""人员变动「调动」的调入机构不能是现属机构（P2-1272，第三十七批「一次请求、一次导入里的重复元素」扫描 AA2-9）。

登记变动框里的调入机构下拉列着本机构，原先选了照收：科室被清空（同处注释写的是「跨机构调动后科室待重新挂接」），
按科室寻医查不到这位医师，变动史多一条「调往本院」。派驻、转诊、会诊、药品调拨都拦「两端同一机构」。修后 422，
提示本机构内换科室用「挂科室」；跨机构调动照旧。
"""
import pytest

M = "/api/mgmt"
SAME_ORG = "调入机构与现属机构不能相同；本机构内换科室请用「挂科室」"


@pytest.fixture(scope="module")
def world(client, admin):
    county = client.post("/api/organizations", headers=admin, json={
        "name": "P1272 县人民医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    branch = client.post("/api/organizations", headers=admin, json={
        "name": "P1272 城东卫生院", "org_type": "township", "level": "township"}).json()["id"]
    dept = client.post(f"{M}/departments", headers=admin, json={
        "org_id": county, "code": "P1272-XNK", "name": "P1272心内科"})
    assert dept.status_code == 201, dept.text
    emps = {}
    for name in ("王主任", "李主任"):
        made = client.post(f"{M}/employees", headers=admin, json={
            "org_id": county, "name": f"P1272 {name}", "title": "主任医师"})
        assert made.status_code in (200, 201), made.text
        emps[name] = made.json()["id"]
        hooked = client.post(f"{M}/employees/{emps[name]}/department?dept_id={dept.json()['id']}", headers=admin)
        assert hooked.status_code == 200, hooked.text
    return {"county": county, "branch": branch, "dept": dept.json()["id"], "emps": emps}


def _employee(client, admin, org_id, employee_id):
    return next(e for e in client.get(f"{M}/employees", headers=admin, params={"org_id": org_id}).json()
                if e["id"] == employee_id)


def _found(client, admin):
    return sorted(d["name"] for d in client.get("/api/appointments/doctors", headers=admin,
                                                params={"keyword": "P1272心内科"}).json())


def test_调入机构选成本机构_422_科室与变动史不动(client, admin, world):
    emp = world["emps"]["王主任"]
    resp = client.post(f"{M}/employees/{emp}/changes", headers=admin, json={
        "change_type": "transfer", "to_org_id": world["county"], "detail": "调心内二病区"})
    assert resp.status_code == 422, resp.text   # 修前 201
    assert resp.json()["detail"] == SAME_ORG
    row = _employee(client, admin, world["county"], emp)
    assert (row["org_id"], row["dept_id"]) == (world["county"], world["dept"])   # 修前科室被清空
    assert client.get(f"{M}/employees/{emp}/changes", headers=admin).json() == []   # 修前多一条「调往本院」
    assert "P1272 王主任" in _found(client, admin)   # 修前按科室寻医查不到他


def test_跨机构调动照旧(client, admin, world):
    emp = world["emps"]["李主任"]
    resp = client.post(f"{M}/employees/{emp}/changes", headers=admin, json={
        "change_type": "transfer", "to_org_id": world["branch"], "detail": "下沉支援"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["employee_org_id"] == world["branch"]
    row = _employee(client, admin, world["branch"], emp)
    assert (row["org_id"], row["dept_id"]) == (world["branch"], None)   # 跨机构调动后科室待重新挂接，照旧
    assert [(c["change_type"], c["to_org_id"]) for c in
            client.get(f"{M}/employees/{emp}/changes", headers=admin).json()] == [("transfer", world["branch"])]
