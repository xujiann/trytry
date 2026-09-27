"""医生移动端的转诊待办按「该谁办」数（P2-532，第十批「同源数字承诺」扫描 X2-3）。

移动端工作台的 docstring：「卫生院看审核与承接，县级看接收与督办」。审核 / 接收只有本单**当前机构的直接上级**推得动
（`referral._assert_review_authority`），承接下转只有当前持有机构办得了（`_assert_holds_case`）。三项待办原先与超时
督办共用「发起机构或当前机构是本机构」：村卫生室交上来、等卫生院审核的单子，当前机构还是村卫生室——卫生院的
「待复核转诊」恒为 0；卫生院自己审过、等县里接收的，倒记进卫生院的「待接收转诊」，点进去审核 403；县里的
「待接收」同样恒为 0。

修后三项待办照推进权限数；「我发起的」与超时督办口径不变。
"""
import pytest

B = "/api/spd"


def _login(client, username):
    token = client.post("/api/auth/login", json={"username": username, "password": "pass123456"}).json()
    return {"Authorization": f"Bearer {token['access_token']}"}


@pytest.fixture(scope="module")
def world(client, admin):
    county = client.post("/api/organizations", headers=admin, json={
        "name": "P2532 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    town = client.post("/api/organizations", headers=admin, json={
        "name": "P2532 卫生院", "org_type": "township", "level": "township", "parent_id": county}).json()["id"]
    village = client.post("/api/organizations", headers=admin, json={
        "name": "P2532 村卫生室", "org_type": "village", "level": "village", "parent_id": town}).json()["id"]
    h = {}
    for key, org in (("county", county), ("town", town), ("village", village)):
        created = client.post("/api/users", headers=admin, json={
            "username": f"p2532_{key}", "password": "pass123456", "role": "doctor", "org_id": org})
        assert created.status_code == 201, created.text
        h[key] = _login(client, f"p2532_{key}")
    patients = []
    for i in range(2):
        pid = client.post("/api/patients", headers=admin, json={
            "name": f"P2532 患者{i}", "id_card": f"33012719580303253{i}"}).json()["id"]
        resp = client.post(f"{B}/enrollments", headers=admin, json={
            "patient_id": pid, "program_code": "hypertension", "org_id": village})
        assert resp.status_code == 201, resp.text
        patients.append(pid)
    return {"orgs": {"county": county, "town": town, "village": village}, "h": h, "patients": patients}


def _todo(client, headers):
    resp = client.get(f"{B}/workbench/doctor-mobile", headers=headers)
    assert resp.status_code == 200, resp.text
    r = resp.json()["referrals"]
    return r["pending_review"], r["pending_accept"], r["pending_receive"]


def test_转诊待办记在该推进的那家机构名下(client, world):
    h, orgs = world["h"], world["orgs"]
    waiting_town = client.post(f"{B}/referrals", headers=h["village"], json={
        "patient_id": world["patients"][0], "program_code": "hypertension", "target_org_id": orgs["county"],
        "reason": "P2532 待卫生院审核"})
    assert waiting_town.status_code == 201, waiting_town.text
    waiting_county = client.post(f"{B}/referrals", headers=h["village"], json={
        "patient_id": world["patients"][1], "program_code": "hypertension", "target_org_id": orgs["county"],
        "reason": "P2532 待县医院接收"})
    assert client.post(f"{B}/referrals/{waiting_county.json()['id']}/review", headers=h["town"],
                       json={"action": "pass"}).status_code == 200

    # 卫生院：该审的 1 张（修前 0），不该它接收（修前把自己审过的那张算成 1，点进去 403）
    assert _todo(client, h["town"]) == (1, 0, 0)
    # 县医院：该接收的 1 张（修前 0）
    assert _todo(client, h["county"]) == (0, 1, 0)
    assert client.post(f"{B}/referrals/{waiting_county.json()['id']}/review", headers=h["county"],
                       json={"action": "pass"}).status_code == 200
    # 接收之后县里到院、下转回卫生院：卫生院待承接 1 张
    assert client.post(f"{B}/referrals/{waiting_county.json()['id']}/arrive", headers=h["county"],
                       json={"effective_visit": True}).status_code == 200
    assert client.post(f"{B}/referrals/{waiting_county.json()['id']}/down", headers=h["county"],
                       json={"target_org_id": orgs["town"]}).status_code == 200
    assert _todo(client, h["town"]) == (1, 0, 1)
    assert _todo(client, h["county"]) == (0, 0, 0)
