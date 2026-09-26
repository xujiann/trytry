"""请求体里的机构编号过了写权限守卫也得查存在：填错的编号报「机构不存在」，不翻成重复登记（P2-169）。

`assert_org_writable` 只管「能不能以这家机构的名义写」——全域角色直接放行，不查机构在不在。只靠它的几处写接口，
机构编号填错一位就撞外键，被各自的 `except IntegrityError`（本是为唯一约束写的）翻成「该设备序列号已登记」
「专家已存在」「该用户已建村医档案」，批量开通村医则记成「并发写入冲突，已跳过」——照着提示查重，查不出任何重复。
P1-90 的闸门把守卫里用过 `body.org_id` 当成「看过」，于是漏了这几处；判据已收紧（见 test_body_fk_exists.py）。
"""
import pytest

MISSING = 987654321


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2169 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    users = []
    for i in (1, 2):
        created = client.post("/api/users", headers=admin, json={
            "username": f"p2169_vd{i}", "password": "passw0rd1", "full_name": f"P2169 村医{i}", "role": "doctor",
            "org_id": org})
        assert created.status_code in (200, 201), created.text
        users.append(created.json()["id"])
    return {"org": org, "users": users}


def test_登记设备_机构不存在是404(client, admin, world):
    resp = client.post("/api/spd/devices", headers=admin, json={"sn": "P2169-SN1", "device_type": "bp", "org_id": MISSING})
    assert (resp.status_code, resp.json()["detail"]) == (404, "机构不存在")   # 修前 409「该设备序列号已登记」
    ok = client.post("/api/spd/devices", headers=admin, json={"sn": "P2169-SN1", "device_type": "bp", "org_id": world["org"]})
    assert ok.status_code == 201, ok.text   # 同一个序列号照常登记得上：之前那次一行没写


def test_建会诊专家_机构不存在是404(client, admin, world):
    resp = client.post("/api/consultations/experts", headers=admin, json={"name": "P2169 专家", "org_id": MISSING})
    assert (resp.status_code, resp.json()["detail"]) == (404, "机构不存在")   # 修前 409「专家已存在」
    ok = client.post("/api/consultations/experts", headers=admin, json={"name": "P2169 专家", "org_id": world["org"]})
    assert ok.status_code == 201, ok.text


def test_开通村医_机构不存在是404(client, admin, world):
    resp = client.post("/api/spd/village-doctors", headers=admin, json={"user_id": world["users"][0], "org_id": MISSING})
    assert (resp.status_code, resp.json()["detail"]) == (404, "机构不存在")   # 修前 409「该用户已建村医档案」
    ok = client.post("/api/spd/village-doctors", headers=admin, json={"user_id": world["users"][0], "org_id": world["org"]})
    assert ok.status_code == 201, ok.text


def test_批量开通村医_机构不存在逐行报出(client, admin, world):
    resp = client.post("/api/spd/village-doctors/batch", headers=admin, json={"items": [
        {"user_id": world["users"][1], "org_id": MISSING}]})
    assert resp.status_code == 200, resp.text
    # 修前「并发写入冲突，已跳过」
    assert resp.json() == {"created": 0, "skipped": [{"user_id": world["users"][1], "reason": "机构不存在"}]}
