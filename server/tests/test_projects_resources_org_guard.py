"""项目 / 里程碑 / 通用资源的写接口必须校验机构归属（2026-09-11 实测取证后补）。

## 实测取证（修之前，乙院 operator 对甲院的对象）

    PATCH /api/projects/1                    → 200  把甲院项目改成"已中止"、进度改 3
    POST  /api/projects/1/milestones         → 201  往甲院项目里插了一条里程碑
    POST  /api/projects/milestones/1/done    → 200  替甲院把里程碑标成已完成
    POST  /api/projects/milestones/1/reopen  → 200  又撤销回去
    PATCH /api/resources/1                   → 200  把甲院的超声机改名、改位置
    POST  /api/resources/1/publish           → 200
    POST  /api/resources/1/withdraw          → 200  写上乙院自己的撤回理由

## 最刺眼的仍然是"同一张表上两套口径"

两个文件里，**建**（`create_project` / `register_resource`）一直老老实实
`assert_org_writable`，**改 / 发布 / 撤回**却什么都不校验，连 `user` 形参都没有。
口径在文件里早就定了，只是后写的端点没跟上——这与知情同意书（P0-11）、
住院文书（P0-10）、慢专病任务（P0-12）是同一个形状的第四族。

## 里程碑那两条闸门看不见，因为归属隔着一跳

`project_milestones` 没有 `org_id`，要经 `project_id` 回到 `admin_projects.org_id`
才判得了归属。横向越权闸门的分母是"**直接取的那张表**带机构列"，于是
`complete_milestone` / `reopen_milestone` 整族掉在分母外——它们从来没进过欠账清单，
不是因为没问题，是因为没被数到。

## 本轮**没有**动读侧，这是有意的

`get_project` 仍然不判归属（`_project_readonly`）。项目详情要不要按机构收口是
另一个决定：医共体里县级统筹看下级项目是常态，而本轮的证据全部来自写侧。
下面有一条用例把"读侧未改"显式钉住——**免得哪天顺手改了没人发现**。
"""
import pytest


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client):
    admin = _login(client, "admin", "admin123")
    a = client.post("/api/organizations",
                    json={"name": "项目甲院", "org_type": "lead_hospital", "level": "county"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "项目乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    for uname, org, role in (("pr_op_a", a, "operator"), ("pr_op_b", b, "operator"),
                             ("pr_dir_b", b, "director")):
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": role, "org_id": org["id"]},
                    headers=admin)
    return {"admin": admin, "a": a, "b": b,
            "op_a": _login(client, "pr_op_a"), "op_b": _login(client, "pr_op_b"),
            "dir_b": _login(client, "pr_dir_b")}


def _new_project(client, w, name="甲院信息化项目"):
    r = client.post("/api/projects",
                    json={"org_id": w["a"]["id"], "name": name, "category": "general"},
                    headers=w["op_a"])
    assert r.status_code == 201, r.text
    return r.json()


def _new_milestone(client, w, project_id, name="甲院自己的里程碑"):
    r = client.post(f"/api/projects/{project_id}/milestones",
                    json={"name": name, "due_date": "2026-12-31"}, headers=w["op_a"])
    assert r.status_code == 201, r.text
    return r.json()


_RESOURCE_SEQ = iter(range(1, 999))


def _new_resource(client, w, name="甲院超声机"):
    r = client.post("/api/resources",
                    json={"org_id": w["a"]["id"], "resource_type": "equipment",
                          "code": f"EQ-{next(_RESOURCE_SEQ)}", "name": name},
                    headers=w["op_a"])
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------ 一个方向：无关机构一律 403


def test_别家机构改不了项目(client, world):
    project = _new_project(client, world)
    resp = client.patch(f"/api/projects/{project['id']}",
                        json={"status": "suspended", "progress_pct": 3},
                        headers=world["op_b"])
    assert resp.status_code == 403, resp.text
    after = client.get(f"/api/projects/{project['id']}", headers=world["admin"]).json()
    assert (after["status"], after["progress_pct"]) == ("planning", 0), "被拒了却改成了"


def test_别家机构往项目里插不了里程碑(client, world):
    project = _new_project(client, world, "甲院设备更新项目")
    resp = client.post(f"/api/projects/{project['id']}/milestones",
                       json={"name": "乙院插入的里程碑", "due_date": "2026-12-31"},
                       headers=world["op_b"])
    assert resp.status_code == 403, resp.text


def test_别家机构标不了里程碑完成(client, world):
    """归属隔着一跳的那两条：`project_milestones` 没有 `org_id`。"""
    project = _new_project(client, world, "甲院基建项目")
    milestone = _new_milestone(client, world, project["id"])
    assert client.post(f"/api/projects/milestones/{milestone['id']}/done",
                       headers=world["op_b"]).status_code == 403


def test_别家机构撤销不了里程碑完成(client, world):
    project = _new_project(client, world, "甲院培训项目")
    milestone = _new_milestone(client, world, project["id"])
    assert client.post(f"/api/projects/milestones/{milestone['id']}/done",
                       headers=world["op_a"]).status_code == 200
    assert client.post(f"/api/projects/milestones/{milestone['id']}/reopen",
                       headers=world["op_b"]).status_code == 403


def test_别家机构改不了资源(client, world):
    resource = _new_resource(client, world)
    resp = client.patch(f"/api/resources/{resource['id']}",
                        json={"name": "被乙院改名了", "location": "乙院机房"},
                        headers=world["op_b"])
    assert resp.status_code == 403, resp.text


def test_别家机构发布不了资源(client, world):
    resource = _new_resource(client, world, "甲院呼吸机")
    assert client.post(f"/api/resources/{resource['id']}/publish",
                       headers=world["op_b"]).status_code == 403


def test_别家机构撤回不了资源(client, world):
    resource = _new_resource(client, world, "甲院会议室")
    assert client.post(f"/api/resources/{resource['id']}/publish",
                       headers=world["op_a"]).status_code == 200
    resp = client.post(f"/api/resources/{resource['id']}/withdraw",
                       json={"reason": "乙院说停用"}, headers=world["op_b"])
    assert resp.status_code == 403, resp.text


# ------------------------------------------------ 另一个方向：不能变成"全关了"


def test_本机构照常(client, world):
    """没有这一条，"修好了"可能只是"谁都动不了了"。"""
    project = _new_project(client, world, "甲院本院项目")
    assert client.patch(f"/api/projects/{project['id']}",
                        json={"progress_pct": 50}, headers=world["op_a"]).status_code == 200
    milestone = _new_milestone(client, world, project["id"], "本院里程碑")
    assert client.post(f"/api/projects/milestones/{milestone['id']}/done",
                       headers=world["op_a"]).status_code == 200
    assert client.post(f"/api/projects/milestones/{milestone['id']}/reopen",
                       headers=world["op_a"]).status_code == 200
    resource = _new_resource(client, world, "甲院本院资源")
    assert client.patch(f"/api/resources/{resource['id']}",
                        json={"location": "三楼"}, headers=world["op_a"]).status_code == 200
    assert client.post(f"/api/resources/{resource['id']}/publish",
                       headers=world["op_a"]).status_code == 200
    assert client.post(f"/api/resources/{resource['id']}/withdraw",
                       json={"reason": "检修"}, headers=world["op_a"]).status_code == 200


def test_全域角色跨机构照常(client, world):
    """`director` 属 GLOBAL_ROLES：县级中心统筹督办各院项目是设计内的。

    隔离做过头会挡掉真实业务，那比不做隔离更糟——它会让人把整套隔离关掉。
    """
    project = _new_project(client, world, "甲院受县级督办的项目")
    assert client.patch(f"/api/projects/{project['id']}",
                        json={"progress_pct": 60}, headers=world["dir_b"]).status_code == 200


# ------------------------------------------------ 顺序与范围


def test_对别家先403不泄露状态(client, world):
    """归属判定排在业务状态机之前。

    否则 409「该资源已发布」/422「结项须把进度报到 100%」这类措辞，
    会把别家单据的当前状态直接说出来——拒绝本身不该是一条信息通道。
    """
    resource = _new_resource(client, world, "甲院已发布资源")
    assert client.post(f"/api/resources/{resource['id']}/publish",
                       headers=world["op_a"]).status_code == 200
    again = client.post(f"/api/resources/{resource['id']}/publish", headers=world["op_b"])
    assert again.status_code == 403, "本院重复发布才该是 409，别家机构应当先被 403 挡下"

    project = _new_project(client, world, "甲院待结项项目")
    resp = client.patch(f"/api/projects/{project['id']}",
                        json={"status": "done"}, headers=world["op_b"])
    assert resp.status_code == 403, "本院进度不满才该是 422，别家机构应当先被 403 挡下"


def test_读侧本轮未改是有意的(client, world):
    """项目详情**仍然**跨机构可读。

    这不是漏改：要不要按机构收口项目详情是另一个决定（县级统筹看下级项目是常态），
    而本轮的证据全部来自写侧。把现状钉住，是为了让将来真要改读侧时，
    改动出现在这条用例的 diff 里，而不是悄悄发生。
    """
    project = _new_project(client, world, "甲院可被读到的项目")
    assert client.get(f"/api/projects/{project['id']}",
                      headers=world["op_b"]).status_code == 200
