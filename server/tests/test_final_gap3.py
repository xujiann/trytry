"""终审轮批3：科室库、人员变动/合同/薪酬、预算执行、物资出入库、系统参数、角色变更记录。"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def setup(client, admin):
    lead = client.post(
        "/api/organizations",
        json={"name": "终审HRP总院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    branch = client.post(
        "/api/organizations",
        json={"name": "终审HRP卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    users = {}
    for username, role in [("fg3_dir", "director"), ("fg3_op", "operator"), ("fg3_doc", "doctor")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": lead["id"]},
            headers=admin,
        )
        users[role] = login(client, username, "pass123456")
    employee = client.post(
        "/api/mgmt/employees",
        json={"org_id": lead["id"], "name": "终审员工", "title": "中级", "position": "内科医师"},
        headers=users["director"],
    ).json()
    return {"lead": lead, "branch": branch, "employee": employee, **users}


def test_department_and_assignment(client, setup):
    dir_h = setup["director"]
    dept = client.post(
        "/api/mgmt/departments",
        json={"org_id": setup["lead"]["id"], "code": "NK", "name": "内科", "category": "clinical"},
        headers=dir_h,
    )
    assert dept.status_code == 201, dept.text
    # 同机构编码唯一
    assert (
        client.post(
            "/api/mgmt/departments",
            json={"org_id": setup["lead"]["id"], "code": "NK", "name": "内科二"},
            headers=dir_h,
        ).status_code
        == 409
    )
    other = client.post(
        "/api/mgmt/departments",
        json={"org_id": setup["branch"]["id"], "code": "QK", "name": "全科"},
        headers=dir_h,
    ).json()
    # 跨机构挂接拒绝
    assert (
        client.post(
            f"/api/mgmt/employees/{setup['employee']['id']}/department?dept_id={other['id']}",
            headers=dir_h,
        ).status_code
        == 422
    )
    ok = client.post(
        f"/api/mgmt/employees/{setup['employee']['id']}/department?dept_id={dept.json()['id']}",
        headers=dir_h,
    )
    assert ok.status_code == 200
    listed = client.get(f"/api/mgmt/departments?org_id={setup['lead']['id']}", headers=dir_h).json()
    assert len(listed) == 1 and listed[0]["name"] == "内科"


def test_employee_change_flow(client, setup):
    dir_h = setup["director"]
    emp_id = setup["employee"]["id"]
    # 调动必须指定机构
    assert (
        client.post(
            f"/api/mgmt/employees/{emp_id}/changes",
            json={"change_type": "transfer"},
            headers=dir_h,
        ).status_code
        == 422
    )
    transfer = client.post(
        f"/api/mgmt/employees/{emp_id}/changes",
        json={
            "change_type": "transfer",
            "to_org_id": setup["branch"]["id"],
            "detail": "下沉支援",
            "effective_date": "2026-09-01",
        },
        headers=dir_h,
    )
    assert transfer.status_code == 201
    assert transfer.json()["employee_org_id"] == setup["branch"]["id"]
    leave = client.post(
        f"/api/mgmt/employees/{emp_id}/changes",
        json={"change_type": "leave", "detail": "退休"},
        headers=dir_h,
    )
    assert leave.status_code == 201 and leave.json()["employee_status"] == "left"
    history = client.get(f"/api/mgmt/employees/{emp_id}/changes", headers=dir_h).json()
    assert [c["change_type"] for c in history] == ["transfer", "leave"]


def test_staff_contract_expiring(client, setup):
    dir_h = setup["director"]
    emp_id = setup["employee"]["id"]
    contract = client.post(
        "/api/mgmt/staff-contracts",
        json={
            "employee_id": emp_id,
            "contract_no": "HT2024-001",
            "start_date": "2024-09-01",
            "end_date": "2026-09-30",
        },
        headers=dir_h,
    )
    assert contract.status_code == 201
    # 编号唯一 / 止期校验
    assert (
        client.post(
            "/api/mgmt/staff-contracts",
            json={
                "employee_id": emp_id,
                "contract_no": "HT2024-001",
                "start_date": "2024-09-01",
                "end_date": "2026-09-30",
            },
            headers=dir_h,
        ).status_code
        == 409
    )
    expiring = client.get(
        "/api/mgmt/staff-contracts/expiring?days=60&today=2026-08-11", headers=dir_h
    ).json()
    assert any(c["contract_no"] == "HT2024-001" for c in expiring)


def test_payroll_with_perf_coefficient(client, setup):
    dir_h = setup["director"]
    emp_id = setup["employee"]["id"]
    # 薪酬录入限管理层
    assert (
        client.post(
            "/api/mgmt/payroll",
            json={"employee_id": emp_id, "period": "2026-07", "base_salary": 6000},
            headers=setup["operator"],
        ).status_code
        == 403
    )
    resp = client.post(
        "/api/mgmt/payroll",
        json={
            "employee_id": emp_id,
            "period": "2026-07",
            "base_salary": 6000,
            "perf_bonus": 2000,
            "perf_coefficient": 1.2,
        },
        headers=dir_h,
    )
    assert resp.status_code == 201
    assert resp.json()["total"] == 8400.0  # 6000 + 2000*1.2
    # 同期重复 409
    assert (
        client.post(
            "/api/mgmt/payroll",
            json={"employee_id": emp_id, "period": "2026-07", "base_salary": 6000},
            headers=dir_h,
        ).status_code
        == 409
    )
    listed = client.get("/api/mgmt/payroll?period=2026-07", headers=dir_h).json()
    assert listed["total_amount"] == 8400.0


def test_budget_execution(client, setup):
    dir_h = setup["director"]
    org_id = setup["lead"]["id"]
    assert (
        client.post(
            "/api/mgmt/budgets",
            json={"org_id": org_id, "year": "2026", "category": "income", "amount": 1000000},
            headers=dir_h,
        ).status_code
        == 201
    )
    # 实际收入两笔
    for period, amount in [("2026-01", 300000), ("2026-02", 200000)]:
        client.post(
            "/api/mgmt/finance",
            json={"org_id": org_id, "period": period, "category": "income", "item": "医疗收入", "amount": amount},
            headers=dir_h,
        )
    execution = client.get(
        f"/api/mgmt/budgets/execution?org_id={org_id}&year=2026", headers=dir_h
    ).json()
    assert execution["income"]["budget"] == 1000000
    assert execution["income"]["actual"] == 500000
    assert execution["income"]["execution_pct"] == 50.0
    # 预算调整（同键覆盖）
    adjusted = client.post(
        "/api/mgmt/budgets",
        json={"org_id": org_id, "year": "2026", "category": "income", "amount": 800000},
        headers=dir_h,
    ).json()
    assert adjusted["adjusted"] is True and adjusted["amount"] == 800000


def test_asset_movements(client, setup):
    op = setup["operator"]
    asset = client.post(
        "/api/mgmt/assets",
        json={"org_id": setup["lead"]["id"], "code": "ZC-100", "name": "办公桌", "category": "office", "quantity": 10},
        headers=op,
    ).json()
    aid = asset["id"]
    issue = client.post(
        f"/api/mgmt/assets/{aid}/movements",
        json={"movement_type": "issue", "quantity": 4, "note": "行政科领用"},
        headers=op,
    )
    assert issue.status_code == 201 and issue.json()["asset_quantity"] == 6
    # 超量出库 409
    assert (
        client.post(
            f"/api/mgmt/assets/{aid}/movements",
            json={"movement_type": "issue", "quantity": 100},
            headers=op,
        ).status_code
        == 409
    )
    client.post(
        f"/api/mgmt/assets/{aid}/movements",
        json={"movement_type": "return", "quantity": 2},
        headers=op,
    )
    scrap = client.post(
        f"/api/mgmt/assets/{aid}/movements",
        json={"movement_type": "scrap", "quantity": 8, "note": "全部报废"},
        headers=op,
    )
    assert scrap.json()["asset_status"] == "scrapped"
    # 报废后不可再动
    assert (
        client.post(
            f"/api/mgmt/assets/{aid}/movements",
            json={"movement_type": "inbound", "quantity": 1},
            headers=op,
        ).status_code
        == 409
    )
    history = client.get(f"/api/mgmt/assets/{aid}/movements", headers=op).json()
    assert len(history) == 3


def test_system_params(client, admin, setup):
    assert (
        client.post(
            "/api/mgmt/params",
            json={"key": "portal.verify_lock_seconds", "value": "600", "description": "居民端核验锁定时长"},
            headers=setup["director"],
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/mgmt/params",
            json={"key": "portal.verify_lock_seconds", "value": "600", "description": "居民端核验锁定时长"},
            headers=admin,
        ).status_code
        == 200
    )
    # 覆盖更新
    client.post(
        "/api/mgmt/params", json={"key": "portal.verify_lock_seconds", "value": "300"}, headers=admin
    )
    params = client.get("/api/mgmt/params", headers=admin).json()
    entry = [p for p in params if p["key"] == "portal.verify_lock_seconds"][0]
    assert entry["value"] == "300" and entry["description"] == "居民端核验锁定时长"


def test_role_change_with_log(client, admin, setup):
    created = client.post(
        "/api/users",
        json={"username": "fg3_change", "password": "pass123456", "role": "operator"},
        headers=admin,
    ).json()
    changed = client.patch(
        f"/api/users/{created['id']}/role", json={"role": "doctor"}, headers=admin
    )
    assert changed.status_code == 200 and changed.json()["role"] == "doctor"
    # 变更记录留痕
    logs = client.get(f"/api/users/role-changes?user_id={created['id']}", headers=admin).json()
    assert len(logs) == 1 and logs[0]["old_role"] == "operator" and logs[0]["new_role"] == "doctor"
    # 角色变更后旧令牌吊销：新登录生效为 doctor
    hdr = login(client, "fg3_change", "pass123456")
    me_can = client.post(
        "/api/maternal/children",
        json={"name": "角色测试儿", "birth_date": "2026-01-01"},
        headers=hdr,
    )
    assert me_can.status_code == 201
    # 非管理员不可调角色
    assert (
        client.patch(
            f"/api/users/{created['id']}/role", json={"role": "operator"}, headers=setup["director"]
        ).status_code
        == 403
    )
