"""统计 / 报表类接口按调用方的统计可见范围收口（P0-37）。

合并报表、运行效率、药占比与抗菌药强度、床位统计四个接口都用 `deps.resolve_org_scope` 取机构范围——
而它是**筛选器不是授权器**（`visibility` 模块 docstring 第一段就这么写）：`?org_id=X` 原样变成 `[X]`，
不带就是全域。四个接口除了登录什么都不要，于是 2026-09-24 实测：一家与谁都没有关系的新卫生院，
医生 / 经办照样读到县医院的合并报表（资产、负债、收入、费用）、平均住院日与床位使用率、药占比与
抗菌药物使用强度、床位占用。这与既有两条用例的口径相反：「财务汇总只含可见机构」、
「同医共体成员可见统计汇总」（A 案：统计看本机构 + 同医共体成员；全域角色看全县）。

修法：四处都把 `resolve_org_scope` 的结果收进 `visibility.scope_stats_orgs`（统计可见范围）：
全域角色原样；不带范围收到本机构 + 同分组成员；带了范围只留可见的那部分，全不可见就是 0 家
（统计结果为空而不是全量，与 `resolve_org_scope`「筛出 0 家」同一语义）。
"""
import pytest


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def stats_world(client):
    """甲县医院与丙卫生院同属一个分组；乙卫生院谁都不挨着。甲院有凭证与病区。"""
    admin = _login(client, "admin", "admin123")
    orgs = {}
    for key, name, otype, level in (("a", "统计口径甲县医院", "lead_hospital", "county"),
                                    ("b", "统计口径乙卫生院", "township", "township"),
                                    ("c", "统计口径丙卫生院", "township", "township")):
        orgs[key] = client.post("/api/organizations",
                                json={"name": name, "org_type": otype, "level": level},
                                headers=admin).json()["id"]
    group = client.post("/api/org-groups", json={"name": "统计口径甲片区", "lead_org_id": orgs["a"]},
                        headers=admin).json()
    for key in ("a", "c"):
        r = client.post(f"/api/org-groups/{group['id']}/members", json={"org_id": orgs[key]}, headers=admin)
        assert r.status_code == 201, r.text
    for key in ("b", "c"):
        for role in ("doctor", "operator"):
            r = client.post("/api/users",
                            json={"username": f"p037_{role}_{key}", "password": "pw123456",
                                  "full_name": f"p037_{role}_{key}", "role": role, "org_id": orgs[key]},
                            headers=admin)
            assert r.status_code == 201, r.text
    voucher = client.post("/api/accounting/vouchers",
                          json={"org_id": orgs["a"], "voucher_no": "P037-001", "voucher_date": "2026-08-01",
                                "summary": "统计口径测试收入",
                                "entries": [{"subject_code": "1002", "debit": 128600},
                                            {"subject_code": "4001", "credit": 128600}]},
                          headers=admin)
    assert voucher.status_code == 201, voucher.text
    posted = client.post(f"/api/accounting/vouchers/{voucher.json()['id']}/post", headers=admin)
    assert posted.status_code == 200, posted.text  # 合并报表只统计已过账凭证
    ward = client.post("/api/inpatient/wards", json={"org_id": orgs["a"], "name": "统计口径内科"}, headers=admin)
    assert ward.status_code == 201, ward.text
    bed = client.post("/api/inpatient/beds", json={"ward_id": ward.json()["id"], "bed_no": "P037-01"}, headers=admin)
    assert bed.status_code == 201, bed.text
    return {"orgs": orgs, "admin": admin,
            "h": {f"{role}_{key}": _login(client, f"p037_{role}_{key}")
                  for key in ("b", "c") for role in ("doctor", "operator")}}


PATHS = [
    "/api/accounting/consolidated-statements?period=2026-08",
    "/api/analytics/efficiency?period=2026-08",
    "/api/analytics/drug-use?period=2026-08",
    "/api/inpatient/stats",
]


def _org_ids(payload) -> set[int]:
    """回执里**列表项**带出的机构号（顶层回显的查询参数不算）。"""
    found: set[int] = set()

    def walk(obj, in_list):
        if isinstance(obj, dict):
            if in_list and isinstance(obj.get("org_id"), int):
                found.add(obj["org_id"])
            for v in obj.values():
                walk(v, False)
        elif isinstance(obj, list):
            for x in obj:
                walk(x, True)

    walk(payload, False)
    return found


def _get(client, headers, path, org_id=None):
    sep = "&" if "?" in path else "?"
    r = client.get(path + (f"{sep}org_id={org_id}" if org_id else ""), headers=headers)
    assert r.status_code == 200, (path, r.text)
    return _org_ids(r.json())


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("who", ["doctor_b", "operator_b"])
def test_无关机构看不到别家的统计(client, stats_world, path, who):
    a, h = stats_world["orgs"]["a"], stats_world["h"][who]
    assert a not in _get(client, h, path), f"{who} 不带机构号就看到了甲院：{path}"
    assert a not in _get(client, h, path, a), f"{who} 带上甲院的机构号就看到了甲院：{path}"


@pytest.mark.parametrize("path", PATHS)
def test_同医共体成员照常看得到片区统计(client, stats_world, path):
    """A 案的另一半：同一分组里的丙院看得到甲院——隔离不能把医共体的管理职能关掉。"""
    a, h = stats_world["orgs"]["a"], stats_world["h"]["doctor_c"]
    assert a in _get(client, h, path), path
    assert a in _get(client, h, path, a), path


@pytest.mark.parametrize("path", PATHS)
def test_全域角色照常看全县(client, stats_world, path):
    assert stats_world["orgs"]["a"] in _get(client, stats_world["admin"], path), path
