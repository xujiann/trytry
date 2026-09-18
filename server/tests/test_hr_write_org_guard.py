"""HR 写端点的机构归属校验（2026-09-18 实测取证后补，P1-59）。

覆盖 `admin_mgmt` 与 `staffing` **两个路由**——派驻建档在两处各有一条，
是 ADR-0024 记的"同一张 secondments 表两套端点"，两条 create 当初都没有归属校验。

## 实测取证（修之前，**乙院 operator** 对甲院的员工）

    POST /api/mgmt/secondments      → 201  把甲院的医师派驻出去（台账里是"甲院 → 乙院"）
    POST /api/mgmt/staff-contracts  → 201  替甲院跟它的医师签了一份劳动合同

两条都只校验"员工存在"就落库。角色守卫回答的是"谁能做"（`require_roles`），
回答不了"能对谁做"——这正是 `visibility.assert_org_writable` docstring 里那句
"这是在替别家做账"的同一形状，只不过这次替别家做的是人事。

## ⚠️ 一处要写清楚的更正：`director` 跨机构是**设计如此**，不是缺陷

`visibility.GLOBAL_ROLES = {"admin", "director"}`——院长是全域角色，本来就能跨机构写。
本文件最初取证时用的就是 `director`，于是"修之前 201、修之后还是 201"，
差点把一个**按设计成立的行为**当成没修好的缺陷。用非全域的 `operator` 重跑才看出真相。
下面有一条用例把"director 跨机构照旧放行"**显式钉住**，免得哪天有人读着这份文件
以为那也是洞，顺手把全域角色一起关掉——那会把医共体统筹这条正路堵死。

## `POST /api/mgmt/payroll` 不在"修复"之列，它本来就够不着

它的角色守卫是 `require_roles("director")`，而 director 已是全域角色——
**能调它的角色全都按设计跨机构**，非全域角色连角色门都过不去（403）。
本批仍给它补了同一句归属校验，作用是**纵深防御**：哪天角色门放宽到 operator，
归属校验已经在那儿了，不必再想起来补。下面有用例把这两件事分别钉住。
"""
import pytest


def _login(client, username, password="pw123456"):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def hr_world(client):
    admin = _login(client, "admin", "admin123")
    a = client.post("/api/organizations",
                    json={"name": "人事甲院", "org_type": "lead_hospital", "level": "county"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "人事乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    for uname, org, role in (("hr_op_a", a, "operator"), ("hr_op_b", b, "operator"),
                             ("hr_dir_b", b, "director")):
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": role, "org_id": org["id"]},
                    headers=admin)
    emp_a = client.post("/api/mgmt/employees", headers=admin,
                        json={"org_id": a["id"], "name": "甲院医师", "title": "主治医师",
                              "position": "医师"}).json()["id"]
    return {"admin": admin, "a": a, "b": b, "emp_a": emp_a,
            "op_a": _login(client, "hr_op_a"), "op_b": _login(client, "hr_op_b"),
            "dir_b": _login(client, "hr_dir_b")}


# ---------------------------------------------------------------- 越权反例


def test_别家operator不能把本院医师派驻出去(client, hr_world):
    resp = client.post("/api/mgmt/secondments", headers=hr_world["op_b"],
                       json={"employee_id": hr_world["emp_a"], "to_org_id": hr_world["b"]["id"],
                             "start_date": "2026-03-01"})
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": "无权以该机构名义写入数据"}
    # 被拒之后什么都没落库
    rows = client.get("/api/staffing/secondments", headers=hr_world["admin"]).json()
    assert [r for r in rows if r["employee_id"] == hr_world["emp_a"]] == []


def test_别家operator不能替本院签劳动合同(client, hr_world):
    resp = client.post("/api/mgmt/staff-contracts", headers=hr_world["op_b"],
                       json={"employee_id": hr_world["emp_a"], "contract_no": "HRG-001",
                             "start_date": "2026-01-01", "end_date": "2027-01-01"})
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": "无权以该机构名义写入数据"}
    assert client.get(f"/api/mgmt/staff-contracts?employee_id={hr_world['emp_a']}",
                      headers=hr_world["admin"]).json() == []


# ---------------------------------------------------------------- 正路没被堵死


def test_本院operator照常派驻与签合同(client, hr_world):
    """补守卫不得把合法路径也挡掉——这是每一批越权修复都要有的反向证据。

    **用本用例自己建的员工**，不蹭 `emp_a`：蹭的话这条就隐式依赖"上面两条越权
    确实没写进去"（否则"该员工已在派驻中"409），做变异验证时会跟着一起红，
    分不清是守卫没了还是前面的脏数据。反向证据必须自己独立成立。
    """
    emp = client.post("/api/mgmt/employees", headers=hr_world["admin"],
                      json={"org_id": hr_world["a"]["id"], "name": "甲院正路医师",
                            "title": "主治医师"}).json()["id"]
    sec = client.post("/api/mgmt/secondments", headers=hr_world["op_a"],
                      json={"employee_id": emp, "to_org_id": hr_world["b"]["id"],
                            "start_date": "2026-03-01"})
    assert sec.status_code == 201, sec.text

    con = client.post("/api/mgmt/staff-contracts", headers=hr_world["op_a"],
                      json={"employee_id": emp, "contract_no": "HRG-OK-1",
                            "start_date": "2026-01-01", "end_date": "2027-01-01"})
    assert con.status_code == 201, con.text


def test_全域角色跨机构照旧放行_这是设计不是洞(client, hr_world):
    """`GLOBAL_ROLES = {"admin", "director"}`：院长统筹全医共体人事是正路。

    **本条不是"还没修好"，是"不该修"**——把它一起关掉会堵死医共体统筹。
    取证时正是先用 director 跑出"修了还是 201"，才发现自己用错了角色。
    """
    emp_b = client.post("/api/mgmt/employees", headers=hr_world["admin"],
                        json={"org_id": hr_world["b"]["id"], "name": "乙院护士",
                              "title": "护师"}).json()["id"]
    # 乙院的 director 对**甲院**的员工签合同：按设计放行
    resp = client.post("/api/mgmt/staff-contracts", headers=hr_world["dir_b"],
                       json={"employee_id": hr_world["emp_a"], "contract_no": "HRG-DIR-1",
                             "start_date": "2026-01-01", "end_date": "2027-01-01"})
    assert resp.status_code == 201, resp.text
    assert emp_b  # 仅确保乙院员工建得出来，后面薪酬用例要用


# ---------------------------------------------------------------- 薪酬：角色门在前


def test_薪酬非全域角色过不了角色门(client, hr_world):
    """`require_roles("director")` 先拦下——所以它此前**不是**可被越权的入口。

    修之前实测：operator 收到的就是这句角色错误，不是 201。
    """
    resp = client.post("/api/mgmt/payroll", headers=hr_world["op_b"],
                       json={"employee_id": hr_world["emp_a"], "period": "2026-03",
                             "base_salary": 99999})
    assert resp.status_code == 403, resp.text
    assert "角色" in resp.json()["detail"]


def test_别家operator不能经staffing建档把本院医师派驻出去(client, hr_world):
    """另一条 create：同一张 secondments 表、同一个动作，此前同样没有归属校验。

    ADR-0024 当时就记了"两条 `create` 都没有归属校验，是两套**共有**的缺口"，
    P1-59 那一批只修了 `admin_mgmt` 那条；这条是另一半。
    实测未修前：乙院 operator 调它 201 落库，台账里同样是"甲院 → 乙院"。
    """
    resp = client.post("/api/staffing/secondments", headers=hr_world["op_b"],
                       json={"employee_id": hr_world["emp_a"],
                             "from_org_id": hr_world["a"]["id"],
                             "to_org_id": hr_world["b"]["id"],
                             "start_date": "2026-05-01", "assignment_type": "long_term"})
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": "无权以该机构名义写入数据"}


def test_本院operator经staffing建档照常放行(client, hr_world):
    """反向证据：用自己建的员工，不蹭上面越权用例的数据。"""
    emp = client.post("/api/mgmt/employees", headers=hr_world["admin"],
                      json={"org_id": hr_world["a"]["id"], "name": "甲院staffing医师",
                            "title": "主治医师"}).json()["id"]
    resp = client.post("/api/staffing/secondments", headers=hr_world["op_a"],
                       json={"employee_id": emp, "from_org_id": hr_world["a"]["id"],
                             "to_org_id": hr_world["b"]["id"],
                             "start_date": "2026-05-01", "assignment_type": "long_term"})
    assert resp.status_code == 201, resp.text


def test_守卫校验的是员工现属机构_不是body自报的from_org_id(client, hr_world):
    """自报的 `from_org_id` 挡不住"报成自己家"——所以校验的是员工的真实归属。

    乙院 operator 把 `from_org_id` 填成乙院、`employee_id` 仍填甲院的医师：
    若守卫用的是 body 自报值就会放行，用员工真实归属才拦得住。
    """
    resp = client.post("/api/staffing/secondments", headers=hr_world["op_b"],
                       json={"employee_id": hr_world["emp_a"],
                             "from_org_id": hr_world["b"]["id"],   # 自报成自己家
                             "to_org_id": hr_world["a"]["id"],     # 收方填甲院，避开"派出=接收"那条 422
                             "start_date": "2026-06-01", "assignment_type": "long_term"})
    # 必须**恰好**是归属守卫拦下的：初稿把 to_org_id 也填成乙院，于是撤掉守卫后
    # 仍被"派出与接收机构不能相同"以 422 拦下，用例**为错误的理由通过**了变异验证。
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": "无权以该机构名义写入数据"}


def test_薪酬的归属校验是纵深防御_今天不改变任何行为(client, hr_world):
    """能调它的角色全是全域角色，所以那句 `assert_obj_org_writable` 今天恒放行。

    钉住它的意义在将来：角色门若放宽到 operator，归属校验已经在那儿了。
    """
    resp = client.post("/api/mgmt/payroll", headers=hr_world["dir_b"],
                       json={"employee_id": hr_world["emp_a"], "period": "2026-04",
                             "base_salary": 1000})
    assert resp.status_code == 201, resp.text
