"""慢专病转诊清单每行的 `actions` 按「这位用户此刻能做什么」给：状态之外还看机构（P2-794，第二十一批「页面给出的动作 vs
后端允许的角色与状态」扫描 N4-3）。

推进权写在三个判据里：审核只认本单当前机构的直接上级（`_assert_review_authority`），到院 / 下转 / 随访接收只认当前持有
机构（`_assert_holds_case`），撤回只认发起人（`withdraw_referral`）。管理端与医生移动端原先只按状态摆按钮（P2-580 /
P2-101）：村医发起上转后，自己这一行有「通过 / 退回」，点了 403「仅本单当前机构的上级机构可审核推进该转诊」；县医院
在待卫生院审核的单子上也有，同样 403；县医院接收之后，村医这一行有「登记到院」，403「仅本单当前处理机构可执行该操作」；
移动端发起人又没有「撤回」。修后清单行出参带 `actions`（与三个判据同一份现算），两端按它摆，移动端补上撤回。
"""
from pathlib import Path

import pytest

from conftest import login

B = "/api/spd"
STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def world(client, admin):
    orgs: dict[str, int] = {}
    for key, name, level, org_type, parent in (("county", "P2794 县医院", "county", "lead_hospital", None),
                                               ("town", "P2794 卫生院", "township", "township", "county"),
                                               ("village", "P2794 村卫生室", "village", "village", "town")):
        body = {"name": name, "level": level, "org_type": org_type}
        if parent:
            body["parent_id"] = orgs[parent]
        resp = client.post("/api/organizations", headers=admin, json=body)
        assert resp.status_code == 201, resp.text
        orgs[key] = resp.json()["id"]
    heads = {}
    for key, org in (("county", "county"), ("town", "town"), ("village", "village"), ("village2", "village")):
        username = f"p2794_{key}"
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "full_name": username, "role": "doctor",
            "org_id": orgs[org]})
        assert resp.status_code in (200, 201), resp.text
        heads[key] = login(client, username, "passw0rd1")
    cases = []
    for i in range(2):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P2794 患者{i}", "id_card": f"33012719580303279{i}"}).json()["id"]
        enrolled = client.post(f"{B}/enrollments", headers=admin, json={
            "patient_id": patient, "program_code": "hypertension", "org_id": orgs["village"]})
        assert enrolled.status_code == 201, enrolled.text
        case = client.post(f"{B}/referrals", headers=heads["village"], json={
            "patient_id": patient, "program_code": "hypertension", "target_org_id": orgs["county"],
            "reason": f"P2794 血压控制不佳 {i}"})
        assert case.status_code == 201, case.text
        cases.append(case.json()["id"])
    return {"orgs": orgs, "heads": heads, "cases": cases}


def _actions(client, headers, case_id):
    rows = client.get(f"{B}/referrals", headers=headers, params={"limit": 200}).json()
    return {r["id"]: r["actions"] for r in rows}.get(case_id)


def test_待卫生院审核_只有卫生院能审_只有发起人能撤(client, world):
    heads, case = world["heads"], world["cases"][0]
    assert _actions(client, heads["village"], case) == ["withdraw"]   # 修前没有这个键；页面按状态给了「通过 / 退回」
    assert _actions(client, heads["village2"], case) == []   # 同机构的另一位村医：不是发起人，也不是上级
    assert _actions(client, heads["county"], case) == []   # 目标机构看得见，但还轮不到它审（修前页面给「通过 / 退回」）
    # 该审的卫生院在清单里看不见这张单：审核方的可见范围是 P1-170（待裁定），不在本条；它按单号直接审照旧 200（下一条）
    assert _actions(client, heads["town"], case) is None
    # 与接口同口径
    denied = client.post(f"{B}/referrals/{case}/review", headers=heads["village"], json={"action": "reject"})
    assert denied.status_code == 403, denied.text
    denied = client.post(f"{B}/referrals/{case}/review", headers=heads["county"], json={"action": "reject"})
    assert denied.status_code == 403, denied.text
    assert client.post(f"{B}/referrals/{case}/withdraw", headers=heads["village2"]).status_code == 403


def test_县医院接收之后_只有县医院能登记到院与下转(client, world):
    heads, case = world["heads"], world["cases"][0]
    assert client.post(f"{B}/referrals/{case}/review", headers=heads["town"],
                       json={"action": "pass", "opinion": "同意上转"}).status_code == 200
    assert _actions(client, heads["county"], case) == ["review"]
    assert _actions(client, heads["town"], case) == [] and _actions(client, heads["village"], case) == []
    assert client.post(f"{B}/referrals/{case}/review", headers=heads["county"],
                       json={"action": "pass", "opinion": "接收"}).status_code == 200
    assert _actions(client, heads["county"], case) == ["arrive", "down"]
    assert _actions(client, heads["village"], case) == []   # 修前页面给「登记到院」
    denied = client.post(f"{B}/referrals/{case}/arrive", headers=heads["village"], json={"effective_visit": True})
    assert denied.status_code == 403, denied.text
    arrived = client.post(f"{B}/referrals/{case}/arrive", headers=heads["county"], json={"effective_visit": True})
    assert arrived.status_code == 200, arrived.text


def test_发起人撤回_按清单给的动作办得成(client, world):
    heads, case = world["heads"], world["cases"][1]
    assert "withdraw" in _actions(client, heads["village"], case)
    resp = client.post(f"{B}/referrals/{case}/withdraw", headers=heads["village"])
    assert resp.status_code == 200 and resp.json()["status"] == "withdrawn", resp.text
    assert _actions(client, heads["village"], case) == []


def test_两端都按后端给的动作摆按钮_移动端补上撤回():
    admin_js = (STATIC / "pages-spd.js").read_text(encoding="utf-8")
    assert "const SPD_REF_OPS" not in admin_js   # 修前按状态自己摆
    assert "const on = (op) => (c.actions || []).includes(op);" in admin_js
    doctor_js = (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")
    ops = doctor_js[doctor_js.index("function spdReferralOps(r)"):]
    ops = ops[:ops.index("\n}\n")]
    assert "const on = (op) => (r.actions || []).includes(op);" in ops
    assert 'on("withdraw")' in ops and "data-spd-withdraw" in ops   # 修前移动端没有撤回
    assert "/withdraw`" in doctor_js
