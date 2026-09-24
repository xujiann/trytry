"""医废扫码追溯按所属机构判可见，转运人员工作量按统计范围收口（P0-39，P1-77 判据下实测）。

医废清单早就只给看本机构（`scope_org_list`，`test_机构维度管理数据不可跨机构读取` 里的「医废清单」），
收集、入暂存、交接也都判了归属——可按追溯码查的 `GET /api/medwaste/trace/{code}` 什么都不看，
而追溯码是 `MW-日期-四位序号`，**顺着日期和序号就能把全县的医废逐包翻出来**：类别、重量、产生点与暂存间、
交接时间与经手人姓名。2026-09-24 实测：一家与谁都没有关系的新卫生院，经办 / 医生按码读到别家的追溯链，200。
它不在任何一道读侧棘轮的分母里：不 `db.get`，直接按路径参数查表（P1-77 量出的那一族）。

同文件的转运人员工作量（`GET /api/medwaste/handler-stats`）是统计：不收任何范围，全县各家的转运
人员姓名与交接批次、重量汇在一张表里——与 P0-37 那四个统计接口同一个形状，照第九轮 A 案收进
统计可见范围（本机构 + 同医共体；全域角色看全县）。

修法：追溯按医废的所属机构判 `assert_org_visible`（本机构；全域角色看全县），码不存在照旧 404；
工作量按 `stats_org_ids` 过滤。
"""
import pytest


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def waste_world(client):
    """甲卫生院收集并交接了一包医废（经手人挂了员工档案）；丙卫生院与甲同属一个分组；乙谁都不挨着。"""
    admin = _login(client, "admin", "admin123")
    orgs = {}
    for key, name in (("a", "医废追溯甲卫生院"), ("b", "医废追溯乙卫生院"), ("c", "医废追溯丙卫生院")):
        orgs[key] = client.post("/api/organizations",
                                json={"name": name, "org_type": "township", "level": "township"},
                                headers=admin).json()["id"]
    group = client.post("/api/org-groups", json={"name": "医废追溯片区", "lead_org_id": orgs["a"]},
                        headers=admin).json()
    for key in ("a", "c"):
        r = client.post(f"/api/org-groups/{group['id']}/members", json={"org_id": orgs[key]}, headers=admin)
        assert r.status_code == 201, r.text
    for key in orgs:
        for role in ("operator", "doctor"):
            r = client.post("/api/users",
                            json={"username": f"p039_{role}_{key}", "password": "pw123456",
                                  "full_name": f"p039_{role}_{key}", "role": role, "org_id": orgs[key]},
                            headers=admin)
            assert r.status_code == 201, r.text
    h = {f"{role}_{key}": _login(client, f"p039_{role}_{key}") for key in orgs for role in ("operator", "doctor")}

    employee = client.post("/api/mgmt/employees", json={"org_id": orgs["a"], "name": "医废追溯转运员"},
                           headers=h["operator_a"])
    assert employee.status_code == 201, employee.text
    waste = client.post("/api/medwaste",
                        json={"org_id": orgs["a"], "waste_type": "infectious", "weight_kg": 3.5,
                              "collected_date": "2026-09-20"},
                        headers=h["operator_a"])
    assert waste.status_code == 201, waste.text
    handed = client.post(f"/api/medwaste/{waste.json()['id']}/handover",
                         json={"handler_name": "医废追溯转运员", "handler_employee_id": employee.json()["id"]},
                         headers=h["operator_a"])
    assert handed.status_code == 200, handed.text
    return {"admin": admin, "h": h, "code": waste.json()["trace_code"], "employee_id": employee.json()["id"]}


@pytest.mark.parametrize("who", ["operator_b", "doctor_b", "operator_c"])
def test_别家按追溯码读不到医废的追溯链(client, waste_world, who):
    """追溯是明细：同片区的丙院也看不到（统计看得宽，明细只看本机构）。"""
    r = client.get(f"/api/medwaste/trace/{waste_world['code']}", headers=waste_world["h"][who])
    assert r.status_code == 403, (who, r.status_code, r.text)
    assert "医废追溯转运员" not in r.text


@pytest.mark.parametrize("who", ["operator_a", "doctor_a"])
def test_本机构照常按码追溯(client, waste_world, who):
    r = client.get(f"/api/medwaste/trace/{waste_world['code']}", headers=waste_world["h"][who])
    assert r.status_code == 200, r.text
    assert [s["step"] for s in r.json()["timeline"]] == ["收集", "交接"]


def test_全域角色照常按码追溯(client, waste_world):
    assert client.get(f"/api/medwaste/trace/{waste_world['code']}", headers=waste_world["admin"]).status_code == 200


def test_追溯码不存在照旧404(client, waste_world):
    r = client.get("/api/medwaste/trace/MW-20260101-9999", headers=waste_world["h"]["operator_b"])
    assert r.status_code == 404, r.text


def _handlers(client, headers) -> set[int]:
    r = client.get("/api/medwaste/handler-stats", headers=headers)
    assert r.status_code == 200, r.text
    return {row["employee_id"] for row in r.json()["handlers"]}


@pytest.mark.parametrize("who", ["operator_b", "doctor_b"])
def test_无关机构的转运工作量里没有别家的人(client, waste_world, who):
    assert waste_world["employee_id"] not in _handlers(client, waste_world["h"][who])


@pytest.mark.parametrize("who", ["operator_a", "operator_c"])
def test_本机构与同片区照常看得到转运工作量(client, waste_world, who):
    """A 案的另一半：统计看本机构 + 同医共体。"""
    assert waste_world["employee_id"] in _handlers(client, waste_world["h"][who])


def test_全域角色照常看全县转运工作量(client, waste_world):
    assert waste_world["employee_id"] in _handlers(client, waste_world["admin"])
