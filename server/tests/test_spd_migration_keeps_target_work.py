"""迁入确认收尾原档案时，迁入机构自己挂在原档案上的任务、干预不跟着取消（P2-1338，第三十九批「辖区与归属」扫描 AC2-1）。

迁出待确认期间原档案仍在管：迁入机构派的任务（手工派的、县医院下转过来的「下转承接与随访」、干预派的「干预执行」）按病种
挂的都是原档案。`close_open_work` 收任务、干预只按档案号取——迁入机构一确认，它自己的这些任务全部取消、干预被移除；同一时候
它排的复诊（P2-849）、随访（P1-129）都留着（扫描实测回执 `closed {'tasks': 4, 'interventions': 1}`，四条任务里三条是迁入
机构的）。

修后任务按所属机构认、干预按负责人所在机构认（与复诊按复诊医生所在机构同一判法）：迁入机构的留下，并改挂迁入档案——复诊、
随访按（患者, 病种）认档案，迁入后自然归迁入档案；任务、干预带档案号，不改挂就还挂在已迁出的原档案上，迁入档案日后结案
（`close_open_work` 按档案号收）也收不到它们。原机构的照旧收尾；死亡等不传迁入机构的收尾一字不变。
"""
import pytest

B = "/api/spd"


def _login(client, username):
    token = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    orgs, users, headers = {}, {}, {}
    orgs["county"] = client.post("/api/organizations", headers=admin, json={
        "name": "P21338 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    for key in ("east", "west"):
        orgs[key] = client.post("/api/organizations", headers=admin, json={
            "name": f"P21338 {key}卫生院", "org_type": "township", "level": "township",
            "parent_id": orgs["county"]}).json()["id"]
    orgs["village"] = client.post("/api/organizations", headers=admin, json={
        "name": "P21338 东镇村卫生室", "org_type": "village", "level": "village", "parent_id": orgs["east"]}).json()["id"]
    for key in ("county", "east", "west", "village"):
        made = client.post("/api/users", headers=admin, json={
            "username": f"p21338_{key}", "password": "passw0rd1", "full_name": f"p21338_{key}", "role": "doctor",
            "org_id": orgs[key]})
        assert made.status_code in (200, 201), made.text
        users[key] = made.json()["id"]
        headers[key] = _login(client, f"p21338_{key}")
    return {"orgs": orgs, "users": users, "headers": headers}


def _patient_at_east(client, admin, world, n):
    """东镇在管的高血压患者；四家机构都接诊过（看得见）。"""
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P21338 患者{n}", "id_card": f"33010219630303133{n}"}).json()["id"]
    enrolled = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": world["orgs"]["east"],
        "doctor_user_id": world["users"]["east"]})
    assert enrolled.status_code == 201, enrolled.text
    for key in ("east", "village", "county", "west"):
        served = client.post("/api/encounters", headers=world["headers"][key], json={
            "patient_id": patient, "org_id": world["orgs"][key], "diagnosis_name": "高血压"})
        assert served.status_code in (200, 201), served.text
    return patient, enrolled.json()["id"]


def _down_to_west(client, world, patient):
    """村医上转 → 东镇审核 → 县医院接收、到院 → 下转西镇：生成挂原档案、机构是西镇的「下转承接与随访」。"""
    h = world["headers"]
    case = client.post(f"{B}/referrals", headers=h["village"], json={
        "patient_id": patient, "program_code": "hypertension", "target_org_id": world["orgs"]["county"],
        "reason": "P21338 血压控制差"})
    assert case.status_code == 201, case.text
    case_id = case.json()["id"]
    for key in ("east", "county"):
        passed = client.post(f"{B}/referrals/{case_id}/review", headers=h[key], json={"action": "pass"})
        assert passed.status_code == 200, passed.text
    arrived = client.post(f"{B}/referrals/{case_id}/arrive", headers=h["county"], json={"effective_visit": True})
    assert arrived.status_code == 200, arrived.text
    down = client.post(f"{B}/referrals/{case_id}/down", headers=h["county"],
                       json={"target_org_id": world["orgs"]["west"], "followup_days": 14})
    assert down.status_code == 200, down.text


def _task(client, headers, patient, title, assignee):
    made = client.post(f"{B}/tasks", headers=headers, json={
        "patient_id": patient, "program_code": "hypertension", "title": title, "assignee_id": assignee})
    assert made.status_code == 201, made.text


def _intervene(client, headers, patient, content):
    """下发一条干预并派「干预执行：健康干预」任务（任务机构 = 下发人所在机构，干预负责人 = 下发人）。"""
    made = client.post(f"{B}/interventions", headers=headers, json={
        "patient_ids": [patient], "program_code": "hypertension", "content": content, "create_task": True})
    assert made.status_code == 201, made.text


def _tasks(client, admin, patient):
    rows = client.get(f"{B}/tasks", headers=admin, params={"patient_id": patient, "limit": 50}).json()
    return {(t["title"], t["org_id"]): (t["status"], t["enrollment_id"]) for t in rows}


def _interventions(client, admin, patient):
    rows = client.get(f"{B}/interventions", headers=admin, params={"patient_id": patient}).json()
    return {i["content"]: (i["owner_id"], i["status"], i["enrollment_id"]) for i in rows}


def test_迁入确认_迁入机构的任务与干预留下并改挂迁入档案_原机构的照旧收尾(client, admin, world):
    orgs, users, h = world["orgs"], world["users"], world["headers"]
    patient, old = _patient_at_east(client, admin, world, 1)
    _down_to_west(client, world, patient)
    moved = client.post(f"{B}/enrollments/{old}/lifecycle", headers=h["east"], json={
        "event": "migrate", "reason": "迁居西镇", "target_org_id": orgs["west"]})
    assert moved.status_code == 200, moved.text
    # 待确认期间两家都还在给患者派工作：按病种挂的都是原档案
    _task(client, h["west"], patient, "P21338 迁入前电话首访", users["west"])
    _intervene(client, h["west"], patient, "P21338 西镇低盐饮食指导")
    _task(client, h["east"], patient, "P21338 东镇交接电话", users["east"])
    _intervene(client, h["east"], patient, "P21338 东镇运动指导")

    confirmed = client.post(f"{B}/lifecycle-events/{moved.json()['event_id']}/confirm", headers=h["west"])
    assert confirmed.status_code == 200, confirmed.text
    new = confirmed.json()["incoming_enrollment"]["id"]
    # 只收原机构一侧的三条任务（上转到院跟踪归发起的村卫生室）、一条干预（修前任务 6、干预 2）
    closed = confirmed.json()["closed"]
    assert (closed["tasks"], closed["interventions"]) == (3, 1), closed

    assert _tasks(client, admin, patient) == {
        ("上转患者到院跟踪", orgs["village"]): ("cancelled", old),
        ("P21338 东镇交接电话", orgs["east"]): ("cancelled", old),
        ("干预执行：健康干预", orgs["east"]): ("cancelled", old),
        # 迁入机构的三条留下、改挂迁入档案（修前都是 cancelled、挂原档案）
        ("下转承接与随访", orgs["west"]): ("pending", new),
        ("P21338 迁入前电话首访", orgs["west"]): ("pending", new),
        ("干预执行：健康干预", orgs["west"]): ("pending", new),
    }
    assert _interventions(client, admin, patient) == {
        "P21338 西镇低盐饮食指导": (users["west"], "planned", new),   # 修前 removed、挂原档案
        "P21338 东镇运动指导": (users["east"], "removed", old),
    }

    # 改挂之后由迁入档案的收尾接管：迁入档案登记死亡，留下的任务、干预一并收掉（不改挂就收不到、成了没人收的工作）
    died = client.post(f"{B}/enrollments/{new}/lifecycle", headers=h["west"], json={"event": "death", "reason": "病故"})
    assert died.status_code == 200, died.text
    assert {status for status, _ in _tasks(client, admin, patient).values()} == {"cancelled"}
    assert {status for _, status, _ in _interventions(client, admin, patient).values()} == {"removed"}


def test_死亡照旧把别家机构挂在档案上的任务与干预一并收掉(client, admin, world):
    orgs, users, h = world["orgs"], world["users"], world["headers"]
    patient, enrollment = _patient_at_east(client, admin, world, 2)
    _task(client, h["west"], patient, "P21338 西镇代管电话", users["west"])
    _intervene(client, h["west"], patient, "P21338 西镇代管干预")
    died = client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=h["east"],
                       json={"event": "death", "reason": "病故"})
    assert died.status_code == 200, died.text
    assert _tasks(client, admin, patient) == {
        ("P21338 西镇代管电话", orgs["west"]): ("cancelled", enrollment),
        ("干预执行：健康干预", orgs["west"]): ("cancelled", enrollment),
    }
    assert _interventions(client, admin, patient) == {"P21338 西镇代管干预": (users["west"], "removed", enrollment)}
