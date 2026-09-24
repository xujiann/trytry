"""离职员工不能派驻：原先照收，派驻结束时「在派 → 在岗」一改，离职的人就回到了在岗医师数里（P1-102）。

`end_secondment` 两条路早按 ADR-0024 改成「只在在派时才回写在岗」，免得把离职的人改回 active；入口这头却还开着。
2026-09-24 开发库实测（修前代码）：员工登记离职（status=left）后，`POST /api/staffing/secondments` 与
`POST /api/mgmt/secondments` 都 201，员工状态被改成 seconded；结束派驻即回写成 active——运行效率的在岗医师数
按 `status == "active"` 计数，这个离职的人重新算了进去。

修法：两条派驻入口对离职员工 409；staffing 那条补录**已经结束**的历史派驻（带结束日期、不动员工状态）照收。
"""
import pytest

from app.database import SessionLocal
from app.models import Employee


@pytest.fixture(scope="module")
def orgs(client, admin):
    return [client.post("/api/organizations", json={"name": f"派驻离职{i}院", "org_type": "township",
                                                    "level": "township"}, headers=admin).json()["id"]
            for i in range(2)]


def _departed(client, admin, org_id, name):
    emp = client.post("/api/mgmt/employees", json={"org_id": org_id, "name": name, "position": "主治医师"},
                      headers=admin).json()
    left = client.post(f"/api/mgmt/employees/{emp['id']}/changes", json={"change_type": "leave"}, headers=admin)
    assert left.status_code == 201 and left.json()["employee_status"] == "left", left.text
    return emp["id"]


def _status(employee_id):
    db = SessionLocal()
    try:
        return db.get(Employee, employee_id).status
    finally:
        db.close()


def test_离职员工派驻_两条入口都409_状态仍是离职(client, admin, orgs):
    a, b = orgs
    eid = _departed(client, admin, a, "离职医师甲")
    r1 = client.post("/api/staffing/secondments",
                     json={"employee_id": eid, "from_org_id": a, "to_org_id": b, "start_date": "2026-09-01"},
                     headers=admin)
    assert r1.status_code == 409, r1.text
    r2 = client.post("/api/mgmt/secondments", json={"employee_id": eid, "to_org_id": b, "start_date": "2026-09-01"},
                     headers=admin)
    assert r2.status_code == 409 and r2.json()["detail"] == "该员工已离职，不能派驻", r2.text
    assert _status(eid) == "left"


def test_补录已结束的历史派驻照收_不动离职状态(client, admin, orgs):
    a, b = orgs
    eid = _departed(client, admin, a, "离职医师乙")
    r = client.post("/api/staffing/secondments",
                    json={"employee_id": eid, "from_org_id": a, "to_org_id": b,
                          "start_date": "2026-01-01", "end_date": "2026-06-30"}, headers=admin)
    assert r.status_code == 201, r.text
    assert _status(eid) == "left"


def test_在岗员工派驻与结束照旧(client, admin, orgs):
    a, b = orgs
    emp = client.post("/api/mgmt/employees", json={"org_id": a, "name": "在岗医师丙", "position": "主治医师"},
                      headers=admin).json()
    s = client.post("/api/staffing/secondments",
                    json={"employee_id": emp["id"], "from_org_id": a, "to_org_id": b, "start_date": "2026-09-01"},
                    headers=admin)
    assert s.status_code == 201 and _status(emp["id"]) == "seconded", s.text
    end = client.post(f"/api/staffing/secondments/{s.json()['id']}/end", params={"end_date": "2026-09-20"},
                      headers=admin)
    assert end.status_code == 200 and _status(emp["id"]) == "active", end.text
