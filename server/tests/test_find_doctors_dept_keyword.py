"""便捷寻医的关键字也比科室，结果带科室名（P2-821，第二十二批「页面查询参数 vs 后端」扫描 X4-6）。

说明（`find_doctors`：「按姓名/科室/职称找医师」）与页面占位（「姓名 / 科室 / 职称」）都写能按科室找，ROADMAP 第十二批的
交付说明同样这么写；后端只比姓名、职称、岗位三列——搜「心内科」空表，挂在心内科的医师查不到，结果表也没有科室列。
职工挂的科室在 `Employee.dept_id`（浙#9 科室信息库）。修后关键字按所挂科室的名称一并比，出参末尾加 `dept_name`。
"""
from pathlib import Path

CORE = (Path(__file__).resolve().parents[1] / "app" / "static" / "core.js").read_text(encoding="utf-8")


def test_按科室名找得到挂在该科室的医师_出参带科室名(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2821 县人民医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    dept = client.post("/api/mgmt/departments", headers=admin, json={"org_id": org, "code": "P2821XNK", "name": "P2821心内科"})
    assert dept.status_code == 201, dept.text
    emp = client.post("/api/mgmt/employees", headers=admin, json={
        "org_id": org, "name": "P2821王建国", "title": "主任医师", "position": "医师"})
    assert emp.status_code == 201, emp.text
    other = client.post("/api/mgmt/employees", headers=admin, json={
        "org_id": org, "name": "P2821李医生", "title": "主治医师", "position": "医师"}).json()   # 没挂科室
    hung = client.post(f"/api/mgmt/employees/{emp.json()['id']}/department", headers=admin,
                       params={"dept_id": dept.json()["id"]})
    assert hung.status_code == 200, hung.text

    rows = client.get("/api/appointments/doctors", headers=admin, params={"keyword": "P2821心内科"}).json()
    assert [(r["employee_id"], r["dept_name"]) for r in rows] == [(emp.json()["id"], "P2821心内科")]   # 修前 []
    by_name = client.get("/api/appointments/doctors", headers=admin, params={"keyword": "P2821"}).json()
    assert {r["employee_id"]: r["dept_name"] for r in by_name} == {emp.json()["id"]: "P2821心内科", other["id"]: ""}


def test_寻医结果表有科室列():
    start = CORE.index('$("#doctor-form").onsubmit')
    body = CORE[start:CORE.index("};", start)]
    assert '["医师", "科室", "职称", "岗位", "机构", "可约号源", "近期号源"]' in body   # 修前没有科室列
    assert '<td>${esc(d.dept_name) || "—"}</td>' in body
