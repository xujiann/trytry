"""慢专病转诊县级「退回」与「接收」共用环节名「县级医院接收」：居民卡片写「已退回」，「查看全过程」里却写「县级医院接收：床位紧张…」
（P2-1022，第二十九批「前后端取值表」扫描 E1-3）。

县级医院那一格通过与退回写的是同一个环节名（`review_referral` 按 `_NEXT` 取环节名、`action` 另记 pass / reject），`referral_ends`
靠这个环节名取上转去的机构，存量不改。居民端全过程只印环节名；管理端全轨迹的「动作」列原样印 reject / pass / withdraw。后端自己
知道环节名不等于结论——`referral_ends` 按 `action == "pass"` 取去向、医生端按 `action === "reject"` 找退回意见。

修法：两处轨迹出参补 `action_name`（与转诊页按钮同名：发起 / 通过 / 退回 / 到院 / 下转 / 随访接收 / 撤回）；管理端「动作」列印它，
居民端环节名里没带出结论的补上「（退回）」「（通过）」。
"""
import inspect
import re
from pathlib import Path

import pytest

from app.spd.routers import referral
from app.spd.routers.referral import ReviewIn
from app.spd.service import REFERRAL_ACTION_NAMES

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    county = client.post("/api/organizations", headers=admin, json={
        "name": "P21022 县医院", "org_type": "lead_hospital", "level": "county"}).json()
    township = client.post("/api/organizations", headers=admin, json={
        "name": "P21022 卫生院", "org_type": "township", "level": "township", "parent_id": county["id"]}).json()
    phone = "13800001022"
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21022 居民", "id_card": "330199198502021022", "gender": "男", "birth_date": "1985-02-02",
        "phone": phone}).json()
    enrolled = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient["id"], "program_code": "hypertension", "org_id": township["id"]})
    assert enrolled.status_code == 201, enrolled.text
    case = client.post(f"{B}/referrals", headers=admin, json={
        "patient_id": patient["id"], "program_code": "hypertension", "reason": "血压不达标"})
    assert case.status_code == 201, case.text
    case_id = case.json()["id"]
    for action, opinion in (("pass", "同意上转"), ("reject", "床位紧张，请先在卫生院调整用药")):
        got = client.post(f"{B}/referrals/{case_id}/review", headers=admin, json={"action": action, "opinion": opinion})
        assert got.status_code == 200, got.text
    code = client.post("/api/portal/auth/sms/code", json={"phone": phone}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code}).json()["access_token"]
    resident = {"Authorization": f"Bearer {token}"}
    # 同号码的档案登录时即自动绑上（409「已完成实名绑定」）；没绑上的走实名绑定，两种都行
    bound = client.post("/api/portal/auth/realname", headers=resident, json={
        "name": patient["name"], "id_card": patient["id_card"]})
    assert bound.status_code in (200, 409), bound.text
    return {"case": case_id, "resident": resident}


def test_动作名表盖住所有会写进轨迹的动作():
    literal = set(re.findall(r'_add_step\(db, case, [^,]+, "(\w+)"', inspect.getsource(referral)))
    assert literal == {"submit", "arrive", "down", "receive", "withdraw"}   # 认得出字面量，下面的比对才作数
    review = set(ReviewIn.model_json_schema()["properties"]["action"]["pattern"].strip("^()$").split("|"))
    assert literal | review == set(REFERRAL_ACTION_NAMES)   # 新加一种动作没起名即红


def test_管理端全轨迹_县级那一格写退回(client, admin, world):
    steps = client.get(f"{B}/referrals/{world['case']}", headers=admin).json()["steps"]
    assert [(s["step"], s["action_name"]) for s in steps] == [
        ("发起", "发起"), ("卫生院审核", "通过"), ("县级医院接收", "退回")]   # 修前没有动作名，页面印 reject


def test_居民端全过程_县级那一格写退回(client, world):
    detail = client.get(f"/api/portal/spd/referrals/{world['case']}", headers=world["resident"])
    assert detail.status_code == 200, detail.text
    last = detail.json()["steps"][-1]
    assert (last["step"], last["action_name"], last["opinion"]) == ("县级医院接收", "退回", "床位紧张，请先在卫生院调整用药")


def test_两端页面印动作名():
    admin_page = (STATIC / "pages-spd.js").read_text(encoding="utf-8")
    assert "<td>${esc(s.action_name || s.action)}</td>" in admin_page
    assert "<td>${esc(s.action)}</td>" not in admin_page
    resident_page = (STATIC / "m" / "m.js").read_text(encoding="utf-8")
    assert "s.action_name && !s.step.includes(s.action_name) ? `（${s.action_name}）` : \"\"" in resident_page
