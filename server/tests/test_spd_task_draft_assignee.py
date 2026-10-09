"""无人认领的待接收任务「保存草稿」或「上传佐证」后翻成办理中、责任人仍空，成了谁都接不了的孤儿（P2-1597，第四十七批
扫描 AK3-1）。

修前：`submit_task` 的存草稿分支在补责任人那一行之前就 return，`add_task_evidence` 翻成办理中也不补——之后接收 409
「该任务不处于可接收状态」（只收待接收 / 已超期），工作台「无人认领」与清单「只看无人认领」都不再数它，清单行
`claimable=false`，操作人与同事的「我的待办」里都没有它。同一个接口的提交审核分支与办结（`_finish_task` 的
`coalesce(assignee_id, user.id)`）早就补责任人。

修法：存草稿与补佐证也把空着的责任人补成操作人，放进同一条条件 UPDATE（`coalesce`）——已有责任人的不动，并发里别人
刚接收的不被覆盖成自己。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    from conftest import login

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21597 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21597 患者", "id_card": "330106196204041597", "birth_date": "1962-04-04"}).json()["id"]
    users = {}
    for tag in ("a", "b"):
        made = client.post("/api/users", headers=admin, json={
            "username": f"p21597_{tag}", "password": "passw0rd1", "role": "doctor",
            "full_name": f"P21597 医生{tag.upper()}", "org_id": org})
        assert made.status_code == 201, made.text
        users[tag] = {"id": made.json()["id"], "headers": login(client, f"p21597_{tag}", "passw0rd1")}
    return {"org": org, "patient": patient, **users}


def _task(client, admin, world, title, require_evidence=False):
    made = client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": world["patient"], "org_id": world["org"], "title": title, "task_type": "followup",
        "require_evidence": require_evidence})
    assert made.status_code == 201, made.text
    assert made.json()["status"] == "pending" and made.json()["assignee_id"] is None
    return made.json()["id"]


def _upload(client, headers, task_id, tag):
    upload = client.post("/api/attachments", headers=headers,
                         files={"file": (f"{tag}.png", b"\x89PNG\r\n\x1a\n" + tag.encode(), "image/png")},
                         data={"owner_type": "spd_task", "owner_id": str(task_id)})
    assert upload.status_code == 201, upload.text
    return upload.json()["id"]


def _draft(client, headers, task_id):
    return client.post(f"{B}/tasks/{task_id}/submit", headers=headers,
                       json={"result": {"note": "已电话联系"}, "draft": True})


def _evidence(client, headers, task_id, tag):
    attachment = _upload(client, headers, task_id, tag)
    return client.post(f"{B}/tasks/{task_id}/evidence", headers=headers, json={"attachment_id": attachment})


def _ids(client, headers, query):
    resp = client.get(f"{B}/tasks?{query}&limit=500", headers=headers)
    assert resp.status_code == 200, resp.text
    return {row["id"] for row in resp.json()}


def _unclaimed_consistent(client, headers):
    """工作台「无人认领」计数与清单「只看无人认领」同一句（P2-825），返回清单里的编号。"""
    listed = _ids(client, headers, "unassigned=true")
    board = client.get(f"{B}/workbench/center", headers=headers)
    assert board.status_code == 200, board.text
    assert board.json()["todo"]["unassigned"] == len(listed)
    return listed


@pytest.mark.parametrize("action", ["draft", "evidence"])
def test_无人认领的任务存草稿或补佐证后_责任人是操作人(client, admin, world, action):
    task_id = _task(client, admin, world, f"P21597 无人认领-{action}", require_evidence=action == "evidence")
    doctor_a, doctor_b = world["a"], world["b"]
    assert task_id in _unclaimed_consistent(client, doctor_a["headers"])

    resp = (_draft(client, doctor_a["headers"], task_id) if action == "draft"
            else _evidence(client, doctor_a["headers"], task_id, "p21597-a"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "doing"
    assert resp.json()["assignee_id"] == doctor_a["id"]   # 修前 None：谁都接不了

    assert task_id in _ids(client, doctor_a["headers"], "mine=true&open_only=true")   # 修前不进任何人的待办
    assert task_id not in _ids(client, doctor_b["headers"], "mine=true&open_only=true")
    # 落到了 A 名下，不再算无人认领——工作台计数与清单仍是同一个数
    assert task_id not in _unclaimed_consistent(client, doctor_a["headers"])
    claim = client.post(f"{B}/tasks/{task_id}/claim", headers=doctor_b["headers"])
    assert claim.status_code == 409, claim.text


@pytest.mark.parametrize("action", ["draft", "evidence"])
def test_已有责任人的任务别人存草稿或补佐证_责任人不变(client, admin, world, action):
    task_id = _task(client, admin, world, f"P21597 已接收-{action}", require_evidence=action == "evidence")
    doctor_a, doctor_b = world["a"], world["b"]
    claim = client.post(f"{B}/tasks/{task_id}/claim", headers=doctor_a["headers"])
    assert claim.status_code == 200 and claim.json()["assignee_id"] == doctor_a["id"], claim.text

    resp = (_draft(client, doctor_b["headers"], task_id) if action == "draft"
            else _evidence(client, doctor_b["headers"], task_id, "p21597-b"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "doing"
    assert resp.json()["assignee_id"] == doctor_a["id"]   # coalesce：已有的不被同事覆盖
    assert task_id in _ids(client, doctor_a["headers"], "mine=true&open_only=true")
    assert task_id not in _ids(client, doctor_b["headers"], "mine=true&open_only=true")
