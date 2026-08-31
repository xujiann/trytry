"""补齐**套件级字节捕获覆盖不到**的 9 个端点（2026-08-24 那批治理）。

本轮 12 个模块 118 个端点的取证靠套件级捕获（`tests/capture_plugin.py`）：
加契约前后各跑一遍全套件，逐 (方法,路径,状态) 比对响应字节，结果是
**落在这 12 个模块内的差异 0 处**。

但捕获有个**沉默的缺口**：测试套件没跑过的端点，前后都没有记录，比对当然
显示"一致"——那不是证据，是没证据。对照模块路由清单一查，有 9 个 GET
一次都没被跑到。本文件把它们逐个调起来，钉住键集合。

这不是形式主义：`GET /api/workflows/definitions` 与 `GET /api/materials/purchases`
这类"列表页第一屏"的接口零覆盖，本身就是个值得记下来的事实——它们上线后
如果形状不对，没有任何东西会发现。
"""
import pytest

from app.database import SessionLocal
from app.models import Organization, User
from app.security import hash_password


@pytest.fixture(scope="module")
def auth(client):
    with SessionLocal() as db:
        org = Organization(name="缺口补测院", org_type="hospital", level="county")
        db.add(org)
        db.flush()
        db.add(User(username="gapadmin", password_hash=hash_password("Gap-admin-2026!"),
                    full_name="缺口管理员", role="admin", org_id=org.id))
        db.commit()
        org_id = org.id
    token = client.post("/api/auth/login",
                        json={"username": "gapadmin",
                              "password": "Gap-admin-2026!"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}, org_id


def test_九个零覆盖端点都能调通且形状可钉(client, auth):
    """空集也要钉：列表返回 `[]` 时契约同样生效（模型对不上会 500，不会静默）。

    这九个端点此前**一次都没被任何用例调过**——不是"测过没问题"，是"没测过"。
    """
    headers, _ = auth
    empty_lists = [
        "/api/materials/purchases",
        "/api/outpatient/consent-templates",
        "/api/rbac/modules",
        "/api/surveillance/pathogens",
        "/api/surveillance/resources",
        "/api/workflows/definitions",
        "/api/workflows/instances",
    ]
    for path in empty_lists:
        resp = client.get(path, headers=headers)
        assert resp.status_code == 200, (path, resp.status_code, resp.text[:200])
        assert isinstance(resp.json(), list), path


def test_基金池的两个只读端点(client, auth):
    """`/pools/{id}/prepayments` 与 `/pools/{id}/settlement`——一个是空列表，
    一个在未清算时应给 404 而不是空对象（"还没清算"和"清算结果是零"是两回事）。"""
    headers, _ = auth
    pool = client.post("/api/fund/pools", headers=headers,
                       json={"year": 2026, "insurance_type": "resident",
                             "total_amount": 500000, "prepay_ratio_pct": 70})
    assert pool.status_code == 201, pool.text[:300]
    pool_id = pool.json()["id"]
    # 金额是 Money 列：整数筹资额仍是 int
    assert pool.json()["total_amount"] == 500000
    assert isinstance(pool.json()["total_amount"], int)

    prepayments = client.get(f"/api/fund/pools/{pool_id}/prepayments", headers=headers)
    assert prepayments.status_code == 200 and prepayments.json() == []

    settlement = client.get(f"/api/fund/pools/{pool_id}/settlement", headers=headers)
    assert settlement.status_code == 404
    assert set(settlement.json()) == {"detail"}


def test_有数据时这些列表的键集合(client, auth):
    """空列表钉不住字段。这条给其中三个造数据，把键集合真正钉下来。"""
    headers, org_id = auth

    tpl = client.post("/api/outpatient/consent-templates", headers=headers,
                      json={"consent_type": "surgery", "title": "手术告知",
                            "body": "正文", "version": "v1"})
    assert tpl.status_code == 201
    listed = client.get("/api/outpatient/consent-templates", headers=headers).json()
    assert set(listed[0]) == {"id", "consent_type", "consent_type_name", "title",
                              "body", "version", "active"}

    purchase = client.post("/api/materials/purchases", headers=headers,
                           json={"org_id": org_id, "item_name": "监护仪", "spec": "A型",
                                 "unit": "台", "quantity": 2, "estimated_price": 50000,
                                 "reason": "更新换代"})
    assert purchase.status_code == 201, purchase.text[:300]
    rows = client.get("/api/materials/purchases", headers=headers).json()
    assert set(rows[0]) == {"id", "org_id", "dept_id", "item_name", "spec", "unit",
                            "quantity", "estimated_price", "status", "supplier_id",
                            "contract_no", "contract_amount", "received_quantity"}
    # Money 列：整数预算价仍是 int，不是 50000.0
    assert rows[0]["estimated_price"] == 50000
    assert isinstance(rows[0]["estimated_price"], int)

    resource = client.post("/api/surveillance/resources", headers=headers,
                           json={"org_id": org_id, "resource_type": "material",
                                 "name": "抗病毒药储备", "quantity": 100, "unit": "盒",
                                 "min_quantity": 200})
    assert resource.status_code == 201, resource.text[:300]
    resources = client.get("/api/surveillance/resources", headers=headers).json()
    assert set(resources[0]) == {"id", "org_id", "resource_type", "resource_type_name",
                                 "name", "quantity", "unit", "min_quantity",
                                 "expire_date", "expired", "below_min", "contact",
                                 "location"}
    # 低于储备下限 → below_min 为真；未填有效期 → 不算过期
    assert resources[0]["below_min"] is True and resources[0]["expired"] is False
