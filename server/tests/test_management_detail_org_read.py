"""按 id 读机构管理数据的明细，要按调用方的明细可见范围判归属（P0-38 第一批）。

凭证清单、职工名册、物资清单早就只给看本机构（`scope_org_list`，
`test_stage15_horizontal.py::test_机构维度管理数据不可跨机构读取` 钉着），同文件的写接口也都判了归属——
可顺着 id 读明细的三个 GET 什么都不看：2026-09-24 实测，一家与谁都没有关系的新卫生院，经办 / 医生按 id
读到县医院的**会计凭证**（摘要、分录、借贷金额）、某位职工的**人事变动史**（调动、离职、转正与说明）、
某件物资的**出入库流水**，全部 200。清单拦住的，明细原样放出来——与 P0-19 / P0-20 同一个形状，
只是这回漏的是机构经营与人事数据而不是病历。

修法：照同文件兄弟端点的口径，取出对象后按它的机构判 `assert_org_visible`（明细可见范围：本机构；
全域角色看全县）。不存在照旧 404。
"""
import pytest


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def mgmt_world(client):
    """甲卫生院有一张已记账的凭证、一位有变动记录的职工、一件有出入库的物资；乙卫生院与它毫无关系。"""
    admin = _login(client, "admin", "admin123")
    orgs = {}
    for key, name in (("a", "明细收口甲卫生院"), ("b", "明细收口乙卫生院")):
        orgs[key] = client.post("/api/organizations",
                                json={"name": name, "org_type": "township", "level": "township"},
                                headers=admin).json()["id"]
        for role in ("operator", "doctor"):
            r = client.post("/api/users",
                            json={"username": f"p038_{role}_{key}", "password": "pw123456",
                                  "full_name": f"p038_{role}_{key}", "role": role, "org_id": orgs[key]},
                            headers=admin)
            assert r.status_code == 201, r.text
    heads = {f"{role}_{key}": _login(client, f"p038_{role}_{key}")
             for key in orgs for role in ("operator", "doctor")}

    voucher = client.post("/api/accounting/vouchers",
                          json={"org_id": orgs["a"], "voucher_no": "P038-001", "voucher_date": "2026-08-03",
                                "summary": "明细收口测试购入耗材",
                                "entries": [{"subject_code": "1002", "credit": 4321},
                                            {"subject_code": "5001", "debit": 4321}]},
                          headers=admin)
    assert voucher.status_code == 201, voucher.text

    employee = client.post("/api/mgmt/employees", json={"org_id": orgs["a"], "name": "明细收口职工"},
                           headers=heads["operator_a"])
    assert employee.status_code == 201, employee.text
    change = client.post(f"/api/mgmt/employees/{employee.json()['id']}/changes",
                         json={"change_type": "regularize", "detail": "试用期满考核合格转正",
                               "effective_date": "2026-08-01"},
                         headers=heads["operator_a"])
    assert change.status_code == 201, change.text

    asset = client.post("/api/mgmt/assets",
                        json={"org_id": orgs["a"], "code": "P038-ASSET-1", "name": "明细收口监护仪",
                              "category": "equipment", "quantity": 3},
                        headers=heads["operator_a"])
    assert asset.status_code == 201, asset.text
    movement = client.post(f"/api/mgmt/assets/{asset.json()['id']}/movements",
                           json={"movement_type": "issue", "quantity": 1, "note": "明细收口领用去向"},
                           headers=heads["operator_a"])
    assert movement.status_code == 201, movement.text

    return {
        "admin": admin, "h": heads,
        "urls": {
            "凭证明细": (f"/api/accounting/vouchers/{voucher.json()['id']}", "明细收口测试购入耗材"),
            "人事变动史": (f"/api/mgmt/employees/{employee.json()['id']}/changes", "试用期满考核合格转正"),
            "出入库流水": (f"/api/mgmt/assets/{asset.json()['id']}/movements", "明细收口领用去向"),
        },
    }


LABELS = ["凭证明细", "人事变动史", "出入库流水"]


@pytest.mark.parametrize("label", LABELS)
@pytest.mark.parametrize("who", ["operator_b", "doctor_b"])
def test_无关机构按id读不到别家的管理明细(client, mgmt_world, label, who):
    url, marker = mgmt_world["urls"][label]
    r = client.get(url, headers=mgmt_world["h"][who])
    assert r.status_code == 403, (label, who, r.status_code, r.text)
    assert marker not in r.text


@pytest.mark.parametrize("label", LABELS)
def test_本机构照常读得到(client, mgmt_world, label):
    url, marker = mgmt_world["urls"][label]
    r = client.get(url, headers=mgmt_world["h"]["operator_a"])
    assert r.status_code == 200, (label, r.text)
    assert marker in r.text


@pytest.mark.parametrize("label", LABELS)
def test_全域角色照常读全县(client, mgmt_world, label):
    url, marker = mgmt_world["urls"][label]
    r = client.get(url, headers=mgmt_world["admin"])
    assert r.status_code == 200, (label, r.text)
    assert marker in r.text


@pytest.mark.parametrize("url", ["/api/accounting/vouchers/987654", "/api/mgmt/employees/987654/changes",
                                 "/api/mgmt/assets/987654/movements"])
def test_不存在照旧404(client, mgmt_world, url):
    assert client.get(url, headers=mgmt_world["h"]["operator_b"]).status_code == 404
