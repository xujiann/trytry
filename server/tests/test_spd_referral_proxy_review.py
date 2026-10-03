"""管理层（全域角色）代审一格之后，下一格的推进权落到错误的机构（P2-1190，第三十四批「跨机构协作的两端」扫描 L2-1 ①②）。

分级审核每过一格把 `current_org_id` 推到这一格该由的机构——本单当前机构的直接上级（ADR-0004）：卫生院审核通过后锚在
卫生院，下一格「县级医院接收」只有卫生院的上级县医院能办；县医院接收后锚在县医院，到院、下转由它办。`review_referral`
原先一律取**操作人所在机构**：本机构账号审核时它就是那个上级，没问题；全域角色（admin / director）代审时就错了——
- 不绑机构的管理层代办「卫生院审核」：锚点原地不动、留在村卫生室。县医院点接收 403；卫生院清单里看不到这张单，按单号
  「县级医院接收」却 200，层级写成 county，随后登记到院 200、村医得「有效上转」积分。代办「县级医院接收」同理：锚点
  留在卫生院，县医院登记到院 403，卫生院 200。
- 绑在县医院的管理层代办「卫生院审核」：锚点推到县医院、状态停在「卫生院已审核」，县医院（自己就是当前机构）与卫生院
  （不是县医院的上级）都 403，只有全域角色推得动。
修前实测（scan34 l2/r3）：变体① 代审 200、current=村，县 403、镇 200 得 accepted / current=镇 / level=county；变体②
current=县，县、镇都 403。

修法：全域角色代审「通过」同样推到原 current 的直接上级，与本机构账号审核通过同一个推法；不取操作人所在机构，也不原地
不动。机构树没配上级的（ADR-0004 风险一节：这时只有全域角色推得动）照旧，不绑机构的保留原锚点、不清成 None。
`test_spd_flow.py::test_referral_global_review_keeps_org_anchor` 第二段原先钉着「代审后卫生院仍可继续推进」，钉的正是
本条的错，同批按 ADR-0004 改写。绑县医院的管理层「代基层开单」发起机构记成县医院（L2-1 ③）不在本条。
"""
import pytest

from conftest import login

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    orgs: dict[str, int] = {}
    for key, name, level, org_type, parent in (
        ("county", "P1190 县医院", "county", "lead_hospital", None),
        ("town", "P1190 卫生院", "township", "township", "county"),
        ("village", "P1190 村卫生室", "village", "village", "town"),
        ("orphan", "P1190 未挂上级的村卫生室", "village", "village", None),
    ):
        body = {"name": name, "level": level, "org_type": org_type}
        if parent:
            body["parent_id"] = orgs[parent]
        resp = client.post("/api/organizations", headers=admin, json=body)
        assert resp.status_code == 201, resp.text
        orgs[key] = resp.json()["id"]
    heads = {}
    for key, role, org in (("village", "doctor", "village"), ("town", "doctor", "town"),
                           ("county", "doctor", "county"), ("dir_free", "director", None),
                           ("dir_county", "director", "county")):
        username = f"p1190_{key}"
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "full_name": username, "role": role,
            "org_id": orgs[org] if org else None})
        assert resp.status_code in (200, 201), resp.text
        heads[key] = login(client, username, "passw0rd1")
    patients = {}
    for key, id_card in (("village", "330127196001011190"), ("orphan", "330127196102021191")):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P1190 居民 {key}", "id_card": id_card}).json()["id"]
        enrolled = client.post(f"{B}/enrollments", headers=admin, json={
            "patient_id": patient, "program_code": "hypertension", "org_id": orgs[key]})
        assert enrolled.status_code == 201, enrolled.text
        patients[key] = patient
    return {"orgs": orgs, "heads": heads, "patients": patients}


def _new_case(client, world, headers=None):
    """村医发起一张上转单：发起机构 = 当前机构 = 村卫生室。"""
    resp = client.post(f"{B}/referrals", headers=headers or world["heads"]["village"], json={
        "patient_id": world["patients"]["village"], "program_code": "hypertension",
        "target_org_id": world["orgs"]["county"], "reason": "P1190 血压控制不佳"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["current_org_id"] == world["orgs"]["village"]
    return resp.json()["id"]


def _review(client, headers, case_id):
    return client.post(f"{B}/referrals/{case_id}/review", headers=headers, json={"action": "pass"})


def _actions(client, headers, case_id):
    rows = client.get(f"{B}/referrals", headers=headers, params={"limit": 200}).json()
    return {r["id"]: r["actions"] for r in rows}.get(case_id)


def test_不绑机构的管理层代办卫生院审核_接收落到县医院(client, world):
    orgs, heads = world["orgs"], world["heads"]
    case = _new_case(client, world)
    proxied = _review(client, heads["dir_free"], case)
    assert proxied.status_code == 200, proxied.text
    body = proxied.json()
    assert (body["status"], body["current_org_id"], body["current_level"]) == (
        "township_reviewed", orgs["town"], "township")   # 修前 current 原地不动、仍是村卫生室
    assert _actions(client, heads["county"], case) == ["review"]   # 修前县医院这一行没有「通过 / 退回」
    assert _actions(client, heads["town"], case) == []
    town = _review(client, heads["town"], case)
    assert town.status_code == 403, town.text   # 修前卫生院按单号「县级医院接收」200、层级写成 county
    county = _review(client, heads["county"], case)
    assert county.status_code == 200, county.text   # 修前 403
    assert (county.json()["status"], county.json()["current_org_id"], county.json()["current_level"]) == (
        "accepted", orgs["county"], "county")
    arrive = client.post(f"{B}/referrals/{case}/arrive", headers=heads["town"], json={"effective_visit": True})
    assert arrive.status_code == 403, arrive.text   # 修前卫生院登记到院 200，村医得「有效上转」
    arrive = client.post(f"{B}/referrals/{case}/arrive", headers=heads["county"], json={"effective_visit": True})
    assert arrive.status_code == 200, arrive.text


def test_绑县医院的管理层代办卫生院审核_县医院照常接收(client, world):
    orgs, heads = world["orgs"], world["heads"]
    case = _new_case(client, world)
    proxied = _review(client, heads["dir_county"], case)
    assert proxied.status_code == 200, proxied.text
    assert (proxied.json()["status"], proxied.json()["current_org_id"]) == (
        "township_reviewed", orgs["town"])   # 修前 current 推到操作人所在的县医院
    assert _actions(client, heads["county"], case) == ["review"]
    town = _review(client, heads["town"], case)
    assert town.status_code == 403, town.text
    county = _review(client, heads["county"], case)
    assert county.status_code == 200, county.text   # 修前县医院、卫生院都 403，只有全域角色推得动
    assert (county.json()["status"], county.json()["current_org_id"]) == ("accepted", orgs["county"])


def test_管理层代办县级医院接收_到院由县医院登记(client, world):
    """第二格同一个推法：卫生院审核通过（锚在卫生院）之后由管理层代办接收，锚点推到卫生院的上级县医院。"""
    orgs, heads = world["orgs"], world["heads"]
    for proxy in ("dir_free", "dir_county"):
        case = _new_case(client, world)
        assert _review(client, heads["town"], case).status_code == 200
        proxied = _review(client, heads[proxy], case)
        assert proxied.status_code == 200, proxied.text
        assert (proxied.json()["status"], proxied.json()["current_org_id"], proxied.json()["current_level"]) == (
            "accepted", orgs["county"], "county"), proxy   # 修前不绑机构的这一格锚点留在卫生院
        assert _actions(client, heads["county"], case) == ["arrive", "down"], proxy
        arrive = client.post(f"{B}/referrals/{case}/arrive", headers=heads["town"], json={"effective_visit": True})
        assert arrive.status_code == 403, (proxy, arrive.text)
        arrive = client.post(f"{B}/referrals/{case}/arrive", headers=heads["county"], json={"effective_visit": True})
        assert arrive.status_code == 200, (proxy, arrive.text)


def test_机构树没配上级时_全域代审不把锚点清成None(client, admin, world):
    """没有上级就没有「这一格的机构」，只有全域角色推得动（ADR-0004 风险一节）：照旧保留上一个真实锚点。"""
    orgs = world["orgs"]
    case = client.post(f"{B}/referrals", headers=admin, json={
        "patient_id": world["patients"]["orphan"], "program_code": "hypertension", "reason": "P1190 未挂上级"})
    assert case.status_code == 201, case.text   # admin 不绑机构：发起机构回落到纳管档案所在机构
    assert case.json()["current_org_id"] == orgs["orphan"]
    proxied = _review(client, admin, case.json()["id"])
    assert proxied.status_code == 200, proxied.text
    assert (proxied.json()["status"], proxied.json()["current_org_id"]) == ("township_reviewed", orgs["orphan"])
