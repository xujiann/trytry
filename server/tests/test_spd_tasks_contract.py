"""慢专病路径与任务域（`spd/tasks`）19 个端点的**响应契约**特征化网。

场景经 HTTP API 种出：一条带进入条件的两节点路径（暂停→恢复→完成），一条
无条件路径（手工推进），外加一条手工任务走完 接收→分配→催办→升级→草稿→
提交→退回→再提交→通过 的全链。代表性端点断言完整精确 JSON 与键序。

四处最要紧的判断（加 `response_model` 前后都得成立）：

1. **任务出参有两种形状**：动作类端点回 27 键（无 patient_name/phone），
   清单与详情在**末尾**追加 patient_name、phone 两键——是追加，不是 null 填充。
2. **`/advance` 的两条分支键不同**：恢复分支是 `instance, status, resumed,
   matched`，推进分支是 `instance, status, current_node_key[, next_node[,
   paused_reason]]`——同一个模型靠可选键 + exclude_unset 对齐两种顺序。
3. **办结回执的 `advanced` 是条件键**：路径任务办结才有，普通任务办结整个键
   不出现（不是 null）。
4. **导出行是 int|str 混型单元格**：`assignee_id or ""` 让同一列里 int 与空串
   并存，声明成单一类型就会改字节。
"""
import re
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def auth(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


B = "/api/spd"
ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

TASK_KEYS = ["id", "program_code", "patient_id", "enrollment_id", "instance_id",
             "node_key", "task_type", "title", "org_id", "team_id", "assignee_id",
             "exec_role", "status", "priority", "due_date", "form_code",
             "require_evidence", "form", "result", "evidence", "evidence_urls",
             "urged_count", "escalated", "review_note", "source", "created_at",
             "finished_at"]
INSTANCE_KEYS = ["id", "enrollment_id", "template_id", "template_code", "template_name",
                 "scene", "program_code", "patient_id", "patient_name",
                 "current_node_key", "current_stage", "status", "progress", "overrides",
                 "owner_user_id", "started_at", "finished_at"]
COND = {"field": "risk_level", "op": "==", "value": "high", "label": ""}


def _ts(value):
    assert isinstance(value, str) and ISO_TS.match(value), value
    return value


def _due(days):
    return (date.today() + timedelta(days=days)).isoformat()


def _no_patient(task_row: dict) -> dict:
    """清单/详情形状（29 键）→ 动作回执形状（27 键）。"""
    return {k: v for k, v in task_row.items() if k not in ("patient_name", "phone")}


@pytest.fixture(scope="module")
def world(client, auth):
    org = client.post(
        "/api/organizations",
        json={"name": "契约任务卫生院", "org_type": "township", "level": "township"},
        headers=auth,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "契约任务患者", "id_card": "330666198801010011", "gender": "男",
              "birth_date": "1988-01-01", "phone": "13900001234"},
        headers=auth,
    ).json()
    programs = client.get(f"{B}/programs", headers=auth).json()
    prog = next(p for p in programs if p["code"] == "hypertension")
    enrollment = client.post(
        f"{B}/enrollments",
        json={"patient_id": patient["id"], "program_code": "hypertension",
              "org_id": org["id"]},
        headers=auth,
    )
    assert enrollment.status_code == 201, enrollment.text

    def template(code, name, nodes):
        created = client.post(
            f"{B}/path-templates",
            json={"program_id": prog["id"], "code": code, "name": name},
            headers=auth,
        )
        assert created.status_code == 201, created.text
        node_ids = {}
        for node in nodes:
            resp = client.post(f"{B}/path-templates/{created.json()['id']}/nodes",
                               json=node, headers=auth)
            assert resp.status_code == 201, resp.text
            node_ids[node["key"]] = resp.json()["id"]
        assert client.post(f"{B}/path-templates/{created.json()['id']}/status",
                           json={"status": "published"}, headers=auth).status_code == 200
        return created.json()["id"], node_ids

    ta, nodes_a = template("CT-PATH-A", "契约条件路径", [
        {"key": "ct_n1", "name": "首次随访", "stage": "stage_a", "seq": 1, "due_days": 7},
        {"key": "ct_n2", "name": "风险复评", "stage": "stage_b", "seq": 2, "due_days": 5,
         "enter_condition": [{"field": "risk_level", "op": "==", "value": "high"}]},
    ])
    tb, nodes_b = template("CT-PATH-B", "契约顺行路径", [
        {"key": "ct_m1", "name": "第一步", "seq": 1},
        {"key": "ct_m2", "name": "第二步", "seq": 2},
    ])

    started = client.post(
        f"{B}/path-instances",
        json={"enrollment_id": enrollment.json()["id"], "template_id": ta},
        headers=auth,
    )
    assert started.status_code == 201, started.text
    return {"org": org, "patient": patient, "prog": prog,
            "enrollment": enrollment.json(), "ta": ta, "tb": tb,
            "nodes_a": nodes_a, "nodes_b": nodes_b,
            "instance": started.json(), "instance_keys": list(started.json().keys())}


# ============================================================ 路径实例


def test_启动回执完整精确(client, auth, world):
    created = world["instance"]
    assert world["instance_keys"] == INSTANCE_KEYS
    assert created == {
        "id": created["id"], "enrollment_id": world["enrollment"]["id"],
        "template_id": world["ta"], "template_code": "CT-PATH-A",
        "template_name": "契约条件路径", "scene": "outpatient",
        "program_code": "hypertension", "patient_id": world["patient"]["id"],
        "patient_name": "契约任务患者", "current_node_key": "ct_n1",
        "current_stage": "stage_a", "status": "running", "progress": 0,
        "overrides": {}, "owner_user_id": 1,
        "started_at": _ts(created["started_at"]),
        "finished_at": "",  # 未结束是空串不是 null
    }


def test_实例列表与详情(client, auth, world):
    created = world["instance"]
    rows = client.get(f"{B}/path-instances",
                      params={"enrollment_id": world["enrollment"]["id"]},
                      headers=auth).json()
    assert rows == [created]

    detail = client.get(f"{B}/path-instances/{created['id']}", headers=auth).json()
    assert list(detail.keys()) == INSTANCE_KEYS + ["nodes"]
    node1, node2 = detail["nodes"]
    assert list(node1.keys()) == ["key", "name", "stage", "seq", "dept", "exec_role",
                                  "service_type", "due_days", "timeout_action",
                                  "require_form", "require_evidence", "is_current",
                                  "tasks"]
    assert list(node1["tasks"][0].keys()) == ["id", "status", "assignee_id", "due_date",
                                              "finished_at"]
    assert detail == {
        **created,
        "nodes": [
            {"key": "ct_n1", "name": "首次随访", "stage": "stage_a", "seq": 1, "dept": "",
             "exec_role": "doctor", "service_type": "followup", "due_days": 7,
             "timeout_action": "remind", "require_form": False,
             "require_evidence": False, "is_current": True,
             "tasks": [{"id": node1["tasks"][0]["id"], "status": "pending",
                        "assignee_id": None, "due_date": _due(7), "finished_at": ""}]},
            {"key": "ct_n2", "name": "风险复评", "stage": "stage_b", "seq": 2, "dept": "",
             "exec_role": "doctor", "service_type": "followup", "due_days": 5,
             "timeout_action": "remind", "require_form": False,
             "require_evidence": False, "is_current": False, "tasks": []},
        ],
    }


def test_进入条件校验_未满足分支(client, auth, world):
    check = client.get(f"{B}/path-nodes/{world['nodes_a']['ct_n2']}/enter-check",
                       params={"instance_id": world["instance"]["id"]},
                       headers=auth).json()
    assert list(check.keys()) == ["allowed", "matched", "conditions"]
    assert check == {"allowed": False, "matched": [], "conditions": [COND]}


def test_调整实例回执(client, auth, world):
    patched = client.patch(f"{B}/path-instances/{world['instance']['id']}",
                           json={"overrides": {"ct_n2": {"due_days": 3}}}, headers=auth)
    assert patched.status_code == 200
    assert patched.json() == {**world["instance"], "overrides": {"ct_n2": {"due_days": 3}}}


# ============================================================ 手工任务全链


def test_任务新建回执27键与清单详情29键(client, auth, world):
    created = client.post(
        f"{B}/tasks",
        json={"patient_id": world["patient"]["id"], "title": "契约手工任务",
              "task_type": "followup", "org_id": world["org"]["id"], "due_days": 3,
              "priority": 2, "program_code": "hypertension"},
        headers=auth,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert list(body.keys()) == TASK_KEYS  # 新建回执**没有** patient_name/phone
    assert body == {
        "id": body["id"], "program_code": "hypertension",
        "patient_id": world["patient"]["id"], "enrollment_id": None,
        "instance_id": None, "node_key": "", "task_type": "followup",
        "title": "契约手工任务", "org_id": world["org"]["id"], "team_id": None,
        "assignee_id": None, "exec_role": "", "status": "pending", "priority": 2,
        "due_date": _due(3), "form_code": "", "require_evidence": False, "form": {},
        "result": {}, "evidence": [], "evidence_urls": [], "urged_count": 0,
        "escalated": False, "review_note": "", "source": "manual",
        "created_at": _ts(body["created_at"]), "finished_at": "",
    }

    got = client.get(f"{B}/tasks/{body['id']}", headers=auth).json()
    assert list(got.keys()) == TASK_KEYS + ["patient_name", "phone"]
    assert got == {**body, "patient_name": "契约任务患者", "phone": "13900001234"}

    rows = client.get(f"{B}/tasks",
                      params={"patient_id": world["patient"]["id"], "limit": 200},
                      headers=auth).json()
    mine = next(r for r in rows if r["id"] == body["id"])
    assert list(mine.keys()) == TASK_KEYS + ["patient_name", "phone"]
    assert mine == got
    world["manual"] = body


def test_接收分配催办升级(client, auth, world):
    task = world["manual"]
    claimed = client.post(f"{B}/tasks/{task['id']}/claim", headers=auth)
    assert claimed.status_code == 200
    assert list(claimed.json().keys()) == TASK_KEYS
    assert claimed.json() == {**task, "status": "claimed", "assignee_id": 1}

    assigned = client.post(f"{B}/tasks/{task['id']}/assign",
                           json={"assignee_id": 1, "note": "转派备注"}, headers=auth)
    assert assigned.status_code == 200
    assert assigned.json() == {**claimed.json(), "review_note": "转派备注"}

    urged = client.post(f"{B}/tasks/{task['id']}/urge", headers=auth)
    assert urged.status_code == 200
    assert urged.json() == {**assigned.json(), "urged_count": 1}

    escalated = client.post(f"{B}/tasks/{task['id']}/escalate", headers=auth)
    assert escalated.status_code == 200
    assert escalated.json() == {**urged.json(), "escalated": True, "priority": 2}
    world["manual"] = escalated.json()


def test_草稿提交退回再提交通过_普通任务无advanced键(client, auth, world):
    task = world["manual"]
    draft = client.post(f"{B}/tasks/{task['id']}/submit",
                        json={"result": {"note": "草稿"}, "draft": True}, headers=auth)
    assert draft.status_code == 200
    assert draft.json() == {**task, "status": "doing", "result": {"note": "草稿"}}

    submitted = client.post(f"{B}/tasks/{task['id']}/submit",
                            json={"result": {"note": "完成"}, "note": "请审核"},
                            headers=auth)
    assert submitted.status_code == 200
    assert submitted.json() == {**draft.json(), "status": "submitted",
                                "result": {"note": "完成"}, "review_note": "请审核"}

    rejected = client.post(f"{B}/tasks/{task['id']}/review",
                           json={"approved": False, "note": "材料不足"}, headers=auth)
    assert rejected.status_code == 200
    assert list(rejected.json().keys()) == TASK_KEYS
    assert rejected.json() == {**submitted.json(), "status": "rejected",
                               "review_note": "材料不足"}

    again = client.post(f"{B}/tasks/{task['id']}/submit",
                        json={"result": {"note": "补全"}}, headers=auth)
    assert again.status_code == 200

    passed = client.post(f"{B}/tasks/{task['id']}/review",
                         json={"approved": True, "note": "通过"}, headers=auth)
    assert passed.status_code == 200
    # 非路径任务办结：**没有 advanced 键**（不是 advanced: null）
    assert list(passed.json().keys()) == TASK_KEYS
    assert passed.json() == {**again.json(), "status": "done", "review_note": "通过",
                             "finished_at": _ts(passed.json()["finished_at"])}


# ============================================================ 路径任务与 advance 分支


def test_办结路径任务_暂停恢复完成三条advance分支(client, auth, world):
    instance = world["instance"]
    blocked = client.post(f"{B}/path-instances/{instance['id']}/advance", headers=auth)
    assert blocked.status_code == 409 and set(blocked.json()) == {"detail"}

    detail = client.get(f"{B}/path-instances/{instance['id']}", headers=auth).json()
    n1_task_id = detail["nodes"][0]["tasks"][0]["id"]
    before = client.get(f"{B}/tasks/{n1_task_id}", headers=auth).json()

    done = client.post(f"{B}/tasks/{n1_task_id}/complete", json={}, headers=auth)
    assert done.status_code == 200, done.text
    body = done.json()
    # 路径任务办结：27 键之外**在末尾**追加 advanced；下一节点条件未满足 → 暂停
    assert list(body.keys()) == TASK_KEYS + ["advanced"]
    assert list(body["advanced"].keys()) == ["status", "current_node_key", "next_node",
                                             "paused_reason"]
    assert body == {
        **_no_patient(before), "status": "done", "assignee_id": 1,
        "finished_at": _ts(body["finished_at"]),
        "advanced": {"status": "paused", "current_node_key": "ct_n2",
                     "next_node": "风险复评", "paused_reason": "进入条件未满足"},
    }

    still = client.post(f"{B}/path-instances/{instance['id']}/advance", headers=auth)
    assert still.status_code == 409 and "条件仍未满足" in still.json()["detail"]

    assert client.patch(f"{B}/enrollments/{world['enrollment']['id']}",
                        json={"risk_level": "high"}, headers=auth).status_code == 200
    check = client.get(f"{B}/path-nodes/{world['nodes_a']['ct_n2']}/enter-check",
                       params={"instance_id": instance["id"]}, headers=auth).json()
    assert check == {"allowed": True, "matched": [COND], "conditions": [COND]}

    resumed = client.post(f"{B}/path-instances/{instance['id']}/advance", headers=auth)
    assert resumed.status_code == 200, resumed.text
    # 恢复分支：instance, status, resumed, matched——没有 current_node_key 等键
    assert list(resumed.json().keys()) == ["instance", "status", "resumed", "matched"]
    assert resumed.json() == {
        "instance": {**instance, "overrides": {"ct_n2": {"due_days": 3}},
                     "current_node_key": "ct_n2", "current_stage": "stage_b",
                     "progress": 50},
        "status": "running", "resumed": True, "matched": [COND],
    }

    n2_detail = client.get(f"{B}/path-instances/{instance['id']}", headers=auth).json()
    n2_task_id = n2_detail["nodes"][1]["tasks"][0]["id"]
    n2_before = client.get(f"{B}/tasks/{n2_task_id}", headers=auth).json()
    finished = client.post(f"{B}/tasks/{n2_task_id}/complete", json={}, headers=auth)
    assert finished.status_code == 200, finished.text
    assert list(finished.json()["advanced"].keys()) == ["status", "current_node_key"]
    assert finished.json() == {
        **_no_patient(n2_before), "status": "done", "assignee_id": 1,
        "finished_at": _ts(finished.json()["finished_at"]),
        "advanced": {"status": "completed", "current_node_key": ""},
    }
    ended = client.get(f"{B}/path-instances/{instance['id']}", headers=auth).json()
    assert ended["status"] == "completed" and ended["progress"] == 100
    assert ended["current_node_key"] == "" and _ts(ended["finished_at"])


def test_手工推进分支与批量处理(client, auth, world):
    started = client.post(
        f"{B}/path-instances",
        json={"enrollment_id": world["enrollment"]["id"], "template_id": world["tb"]},
        headers=auth,
    )
    assert started.status_code == 201, started.text
    ib = started.json()
    m1_task_id = client.get(f"{B}/path-instances/{ib['id']}",
                            headers=auth).json()["nodes"][0]["tasks"][0]["id"]

    cancelled = client.post(f"{B}/tasks/batch",
                            json={"task_ids": [m1_task_id], "action": "cancel",
                                  "note": "让路"}, headers=auth)
    assert cancelled.status_code == 200
    assert list(cancelled.json().keys()) == ["processed", "skipped"]
    assert cancelled.json() == {"processed": 1, "skipped": []}

    advanced = client.post(f"{B}/path-instances/{ib['id']}/advance", headers=auth)
    assert advanced.status_code == 200, advanced.text
    # 推进分支：instance, status, current_node_key, next_node——没有 resumed/matched
    assert list(advanced.json().keys()) == ["instance", "status", "current_node_key",
                                            "next_node"]
    assert advanced.json() == {
        "instance": {**ib, "current_node_key": "ct_m2", "current_stage": ""},
        "status": "running", "current_node_key": "ct_m2", "next_node": "第二步",
    }

    m2_task_id = client.get(f"{B}/path-instances/{ib['id']}",
                            headers=auth).json()["nodes"][1]["tasks"][0]["id"]
    mixed = client.post(f"{B}/tasks/batch",
                        json={"task_ids": [m1_task_id, m2_task_id], "action": "claim"},
                        headers=auth)
    assert mixed.status_code == 200
    assert list(mixed.json()["skipped"][0].keys()) == ["id", "reason"]
    assert mixed.json() == {"processed": 1,
                            "skipped": [{"id": m1_task_id, "reason": "任务已结束"}]}
    world["ib"] = ib
    world["m2_task"] = m2_task_id


# ============================================================ 汇总与导出


def test_待办汇总与清单对得上(client, auth, world):
    rows = client.get(f"{B}/tasks", params={"limit": 500}, headers=auth).json()
    open_statuses = ("pending", "claimed", "doing", "submitted", "overdue")
    by_status: dict = {}
    open_by_type: dict = {}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        if row["status"] in open_statuses:
            open_by_type[row["task_type"]] = open_by_type.get(row["task_type"], 0) + 1

    summary = client.get(f"{B}/tasks/summary", headers=auth)
    assert summary.status_code == 200
    body = summary.json()
    assert list(body.keys()) == ["by_status", "open_by_type", "open_total", "overdue",
                                 "escalated", "due_today", "swept"]
    assert list(body["swept"].keys()) == ["overdue", "escalated", "revisits", "followups"]
    assert body == {
        "by_status": by_status, "open_by_type": open_by_type,
        "open_total": sum(1 for r in rows if r["status"] in open_statuses),
        "overdue": by_status.get("overdue", 0),
        "escalated": sum(1 for r in rows if r["escalated"]),
        "due_today": sum(1 for r in rows
                         if r["due_date"] == date.today().isoformat()
                         and r["status"] in open_statuses),
        "swept": {"overdue": 0, "escalated": 0, "revisits": 0, "followups": 0},
    }


def test_导出行是int与str混型单元格(client, auth, world):
    rows = client.get(f"{B}/tasks", params={"limit": 500}, headers=auth).json()
    export = client.get(f"{B}/tasks-export", headers=auth)
    assert export.status_code == 200
    body = export.json()
    assert list(body.keys()) == ["columns", "rows", "total"]
    expected_rows = [
        [r["id"], r["patient_name"], r["program_code"], r["task_type"], r["title"],
         r["status"], r["priority"], r["due_date"],
         r["assignee_id"] if r["assignee_id"] is not None else "",
         r["urged_count"],
         datetime.fromisoformat(r["created_at"]).strftime("%Y-%m-%d %H:%M")]
        for r in sorted(rows, key=lambda r: r["id"], reverse=True)
    ]
    assert body == {
        "columns": ["任务ID", "患者", "病种", "任务类型", "标题", "状态", "优先级",
                    "截止日期", "责任人ID", "催办次数", "创建时间"],
        "rows": expected_rows,
        "total": len(rows),
    }
    # 责任人列里 int 与空串并存：混型单元格是既有字节，声明单一类型会改掉它
    assignee_cells = {type(row[8]) for row in body["rows"]}
    assert assignee_cells == {int, str}


# ============================================================ 错误体


def test_各类错误体都只有detail(client, auth, world):
    manual = world["manual"]["id"]
    cases = [
        (client.post(f"{B}/path-instances",
                     json={"enrollment_id": 999999, "template_id": world["ta"]},
                     headers=auth), 404),
        (client.post(f"{B}/path-instances",
                     json={"enrollment_id": world["enrollment"]["id"],
                           "template_id": 999999}, headers=auth), 404),
        # 顺行路径还在跑：同模板重复启动要 409
        (client.post(f"{B}/path-instances",
                     json={"enrollment_id": world["enrollment"]["id"],
                           "template_id": world["tb"]}, headers=auth), 409),
        (client.get(f"{B}/path-instances/999999", headers=auth), 404),
        (client.patch(f"{B}/path-instances/999999", json={"status": "paused"},
                      headers=auth), 404),
        # 条件路径已完成：不可调整、不可推进
        (client.patch(f"{B}/path-instances/{world['instance']['id']}",
                      json={"status": "paused"}, headers=auth), 409),
        (client.post(f"{B}/path-instances/{world['instance']['id']}/advance",
                     headers=auth), 409),
        (client.get(f"{B}/path-nodes/999999/enter-check",
                    params={"instance_id": world["instance"]["id"]}, headers=auth), 404),
        (client.get(f"{B}/tasks/999999", headers=auth), 404),
        (client.post(f"{B}/tasks",
                     json={"patient_id": world["patient"]["id"], "title": "幽灵档案",
                           "enrollment_id": 999999}, headers=auth), 404),
        # 手工任务已办结：不可再接收/催办/升级/提交/审核
        (client.post(f"{B}/tasks/{manual}/claim", headers=auth), 409),
        (client.post(f"{B}/tasks/{manual}/urge", headers=auth), 409),
        (client.post(f"{B}/tasks/{manual}/escalate", headers=auth), 409),
        (client.post(f"{B}/tasks/{manual}/submit", json={}, headers=auth), 409),
        (client.post(f"{B}/tasks/{manual}/review", json={"approved": True},
                     headers=auth), 409),
        # 已结束的任务不可再分配（先判结束再查人，所以是 409 不是 404）
        (client.post(f"{B}/tasks/{manual}/assign",
                     json={"assignee_id": 999999}, headers=auth), 409),
        # 未结束的任务分配给不存在的人才走 404
        (client.post(f"{B}/tasks/{world['m2_task']}/assign",
                     json={"assignee_id": 999999}, headers=auth), 404),
        (client.post(f"{B}/tasks/batch",
                     json={"task_ids": [manual], "action": "assign"}, headers=auth), 422),
    ]
    for resp, expected in cases:
        assert resp.status_code == expected, f"{resp.request.url} -> {resp.text}"
        assert set(resp.json()) == {"detail"}
