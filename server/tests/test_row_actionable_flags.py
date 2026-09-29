"""清单行上的写按钮按「这一行当前用户能不能写」摆：平台转诊的接诊 / 退回 / 结案只有接收机构能做，缺药登记的流转 / 结案
以登记机构的名义写（P2-793，第二十一批「页面给出的动作 vs 后端允许的角色与状态」扫描 N4-4）。

两份清单都是全县的（P1-69 待裁定），页面原先只看状态摆按钮：卫生院上转到县医院后，自己清单里这张单给「接诊 / 退回」，
点了 403「仅转诊接收机构可推进该单状态」，接收后给「结案」同样 403，不相干的第三家也一样；乙院经办点甲院缺药登记的
「流转」403「无权以该机构名义写入数据」。修法：出参补 `can_advance` / `can_handle`（后端按写接口的同一判据现算），页面
按它摆；按角色摆不摆（转诊只收医师、缺药只收经办 / 药师）随 P2-447 待裁定。
"""
import itertools
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from conftest import login

from app.visibility import assert_org_writable, can_write_org

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {}
    for key, name, level in (("a", "P2793 甲卫生院", "township"), ("b", "P2793 县医院", "county"),
                             ("c", "P2793 丙卫生院", "township")):
        orgs[key] = client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township" if level == "township" else "lead_hospital", "level": level}).json()["id"]
    heads = {}
    for key, role in (("a", "doctor"), ("b", "doctor"), ("c", "doctor"), ("a", "operator"), ("c", "operator")):
        username = f"p2793_{key}_{role}"
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "full_name": username, "role": role, "org_id": orgs[key]})
        assert resp.status_code in (200, 201), resp.text
        heads[f"{key}_{role}"] = login(client, username, "passw0rd1")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2793 患者", "id_card": "330102196002022793"}).json()["id"]
    referral = client.post("/api/referrals", headers=heads["a_doctor"], json={
        "patient_id": patient, "from_org_id": orgs["a"], "to_org_id": orgs["b"], "direction": "up",
        "reason": "P2793 上转"})
    assert referral.status_code == 201, referral.text
    assert referral.json()["can_advance"] is False   # 转出方自己建的单，推进不了
    shortage = client.post("/api/medication/shortages", headers=heads["a_operator"], json={
        "org_id": orgs["a"], "drug_code": "P2793D", "drug_name": "P2793 短缺药", "quantity": 2})
    assert shortage.status_code == 201, shortage.text
    assert shortage.json()["can_handle"] is True
    return {"heads": heads, "referral": referral.json()["id"], "shortage": shortage.json()["id"]}


def _flag(client, headers, path, row_id, key):
    rows = {r["id"]: r for r in client.get(path, headers=headers).json()}
    return rows[row_id][key]


def test_转诊清单_只有接收机构的那一行能推进(client, admin, world):
    heads, ref = world["heads"], world["referral"]
    for who, expected in (("a_doctor", False), ("b_doctor", True), ("c_doctor", False)):
        assert _flag(client, heads[who], "/api/referrals", ref, "can_advance") is expected, who   # 修前没有这个键
    assert _flag(client, admin, "/api/referrals", ref, "can_advance") is True   # 全域角色放行
    # 与写接口同一口径：转出方、第三家 403，接收方 200
    for who in ("a_doctor", "c_doctor"):
        denied = client.patch(f"/api/referrals/{ref}/status", headers=heads[who], json={"status": "accepted"})
        assert denied.status_code == 403, denied.text
    accepted = client.patch(f"/api/referrals/{ref}/status", headers=heads["b_doctor"], json={"status": "accepted"})
    assert accepted.status_code == 200 and accepted.json()["can_advance"] is True, accepted.text
    assert _flag(client, heads["a_doctor"], "/api/referrals?status=accepted", ref, "can_advance") is False   # 结案同理


def test_缺药登记清单_只有登记机构的那一行能流转结案(client, admin, world):
    heads, sid = world["heads"], world["shortage"]
    assert _flag(client, heads["a_operator"], "/api/medication/shortages", sid, "can_handle") is True
    assert _flag(client, heads["c_operator"], "/api/medication/shortages", sid, "can_handle") is False   # 修前没有这个键
    assert _flag(client, admin, "/api/medication/shortages", sid, "can_handle") is True
    denied = client.post(f"/api/medication/shortages/{sid}/advance", headers=heads["c_operator"])
    assert denied.status_code == 403, denied.text
    moved = client.post(f"/api/medication/shortages/{sid}/advance", headers=heads["a_operator"])
    assert moved.status_code == 200 and moved.json()["can_handle"] is True, moved.text


def test_布尔判据与写接口同一口径():
    """`can_write_org` 只给页面判断摆不摆按钮，真正放不放行以 `assert_org_writable` 为准；两者逐个组合必须一致。"""
    for role, user_org, org_id in itertools.product(
            ("admin", "director", "doctor", "operator", "pharmacist", "public_health", "custom_x"),
            (None, 1, 2), (None, 1, 2)):
        user = SimpleNamespace(role=role, org_id=user_org)
        try:
            assert_org_writable(None, user, org_id)
            allowed = True
        except HTTPException:
            allowed = False
        assert can_write_org(user, org_id) is allowed, (role, user_org, org_id)


def test_页面按行上的标记摆按钮():
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    assert 'const actions = !r.can_advance ? "—"' in core   # 修前只看 r.status
    clinical = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    assert 'const canAdvance = s.can_handle && (s.status === "registered" || s.status === "purchasing");' in clinical
    assert 'CLOSED.includes(s.status) || !s.can_handle ? "" :' in clinical
