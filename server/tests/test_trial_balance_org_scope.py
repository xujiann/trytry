"""试算平衡表不带机构号时收进调用方的统计可见范围（P1-155）。

`assert_org_visible` 对不带机构号直接放行，机构筛选又只在 `if org_id is not None` 里——不带机构号就把全县各院的
已过账凭证加在一起：一家与谁都不挨着的卫生院医生照样读到县医院的试算平衡表，而同一个人的凭证清单只给看本机构
（P0-38）、别家的凭证明细 403。同文件的合并报表早按 P0-37 A 案收口：全域角色看全县；不带范围收到本机构 +
同医共体成员；带了范围只留可见的。
"""
import pytest


def _login(client, username, password="pw123456"):
    token = client.post("/api/auth/login", json={"username": username, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    """甲县医院与丙卫生院同属一个分组；乙卫生院谁都不挨着。甲院 8 月有一张已过账凭证。"""
    orgs = {}
    for key, name, otype, level in (("a", "P1155 甲县医院", "lead_hospital", "county"),
                                    ("b", "P1155 乙卫生院", "township", "township"),
                                    ("c", "P1155 丙卫生院", "township", "township")):
        orgs[key] = client.post("/api/organizations", headers=admin,
                                json={"name": name, "org_type": otype, "level": level}).json()["id"]
    group = client.post("/api/org-groups", headers=admin, json={"name": "P1155 片区", "lead_org_id": orgs["a"]}).json()
    for key in ("a", "c"):
        assert client.post(f"/api/org-groups/{group['id']}/members", headers=admin,
                           json={"org_id": orgs[key]}).status_code == 201
    for key in ("b", "c"):
        assert client.post("/api/users", headers=admin, json={
            "username": f"p1155_doc_{key}", "password": "pw123456", "role": "doctor", "org_id": orgs[key]}).status_code == 201
    voucher = client.post("/api/accounting/vouchers", headers=admin, json={
        "org_id": orgs["a"], "voucher_no": "P1155-001", "voucher_date": "2026-08-01", "summary": "试算口径",
        "entries": [{"subject_code": "1002", "debit": 128600}, {"subject_code": "4001", "credit": 128600}]})
    assert voucher.status_code == 201, voucher.text
    assert client.post(f"/api/accounting/vouchers/{voucher.json()['id']}/post", headers=admin).status_code == 200
    return {"b": _login(client, "p1155_doc_b"), "c": _login(client, "p1155_doc_c")}


def _debit_1002(client, headers):
    body = client.get("/api/accounting/trial-balance?period=2026-08", headers=headers)
    assert body.status_code == 200, body.text
    return sum(line["debit"] for line in body.json()["lines"] if line["subject_code"] == "1002")


def test_与谁都不挨着的卫生院看不到县医院的账(client, world):
    assert _debit_1002(client, world["b"]) == 0                 # 修前 128600


def test_同分组成员与全域角色照旧看得到(client, admin, world):
    assert _debit_1002(client, world["c"]) == 128600
    assert _debit_1002(client, admin) == 128600
