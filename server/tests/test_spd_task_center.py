"""任务中心的调度动作：认领、指派、催办、升级、提交、审核、批量、导出。

统一任务表最大的好处是这些动作只实现一次；代价是**一处写错，十种任务一起错**。
既有用例把"任务能不能被建出来、办完能不能推进路径"覆盖住了，
调度动作这一层（中心端每天在点的那些按钮）几乎没有。

护理服务侧（干预 / 复诊 / 个案上报 / 咨询）一并在这里补：
它们与任务共享同一批状态词，改一处很容易漏另一处。
"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def h(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def base(client, h):
    org = client.post(
        "/api/organizations",
        json={"name": "任务中心卫生院", "org_type": "township", "level": "township"},
        headers=h,
    ).json()
    doctor = client.post(
        "/api/users",
        json={"username": "task_doctor", "password": "passw0rd1", "role": "doctor",
              "full_name": "任务医生", "org_id": org["id"]},
        headers=h,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "任务患者", "id_card": "330455199505050066", "gender": "女",
              "birth_date": "1995-05-05", "phone": "13700007777"},
        headers=h,
    ).json()
    enrollment = client.post(
        "/api/spd/enrollments",
        json={"patient_id": patient["id"], "program_code": "hypertension",
              "org_id": org["id"], "risk_level": "mid"},
        headers=h,
    ).json()
    return {"org": org, "doctor": doctor, "patient": patient, "enrollment": enrollment}


def _task(client, h, base, title="调度任务", **kw):
    body = {"patient_id": base["patient"]["id"], "title": title,
            "task_type": "followup", "org_id": base["org"]["id"], "due_days": 7, **kw}
    resp = client.post("/api/spd/tasks", json=body, headers=h)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ============================================================ 单条调度动作


def test_认领后再被别人认领会被拦(client, h, base):
    task = _task(client, h, base, title="待认领")
    claimed = client.post(f"/api/spd/tasks/{task['id']}/claim", headers=h).json()
    assert claimed["status"] == "claimed" and claimed["assignee_id"]
    again = client.post(f"/api/spd/tasks/{task['id']}/claim", headers=h)
    assert again.status_code == 409, "已被认领的任务不该能再认领一次"


def test_指派会把责任人换过去(client, h, base):
    task = _task(client, h, base, title="待指派")
    assigned = client.post(
        f"/api/spd/tasks/{task['id']}/assign",
        json={"assignee_id": base["doctor"]["id"], "note": "转给专管医生"},
        headers=h,
    ).json()
    assert assigned["assignee_id"] == base["doctor"]["id"]
    assert client.post(f"/api/spd/tasks/{task['id']}/assign",
                       json={"assignee_id": 999999}, headers=h).status_code == 404


def test_催办累加次数并通知责任人(client, h, base):
    task = _task(client, h, base, title="待催办")
    client.post(f"/api/spd/tasks/{task['id']}/assign",
                json={"assignee_id": base["doctor"]["id"]}, headers=h)
    first = client.post(f"/api/spd/tasks/{task['id']}/urge", headers=h).json()
    second = client.post(f"/api/spd/tasks/{task['id']}/urge", headers=h).json()
    assert second["urged_count"] == first["urged_count"] + 1, "催办次数要累加，否则看不出催过几回"

    from app.database import SessionLocal
    from app.models import Notification

    with SessionLocal() as db:
        notices = (
            db.query(Notification)
            .filter(Notification.user_id == base["doctor"]["id"],
                    Notification.category == "spd_task")
            .all()
        )
    assert notices, "催办要真的通知到责任人，不能只在任务上加个数字"


def test_升级会提优先级并留痕(client, h, base):
    task = _task(client, h, base, title="待升级", priority=1)
    escalated = client.post(f"/api/spd/tasks/{task['id']}/escalate",
                            json={"note": "两次催办无果"}, headers=h).json()
    assert escalated["escalated"] is True
    assert escalated["priority"] > task["priority"], "升级不提优先级，等于只是改了个标记"


def test_提交后要审核_通过才算办结(client, h, base):
    task = _task(client, h, base, title="待审核")
    submitted = client.post(f"/api/spd/tasks/{task['id']}/submit",
                            json={"result": {"note": "已上门"}}, headers=h).json()
    assert submitted["status"] == "submitted", "提交不等于办结——否则完成率会变成提交率"

    rejected = client.post(f"/api/spd/tasks/{task['id']}/review",
                           json={"approved": False, "note": "缺血压记录"}, headers=h).json()
    assert rejected["status"] == "rejected" and "缺血压记录" in rejected["review_note"]

    resubmit = client.post(f"/api/spd/tasks/{task['id']}/submit",
                           json={"result": {"note": "补了血压 138/86"}}, headers=h)
    assert resubmit.status_code == 200, resubmit.text
    passed = client.post(f"/api/spd/tasks/{task['id']}/review",
                         json={"approved": True}, headers=h).json()
    assert passed["status"] == "done" and passed["finished_at"]


def test_已结束的任务不能再办一次(client, h, base):
    task = _task(client, h, base, title="已办结")
    client.post(f"/api/spd/tasks/{task['id']}/complete",
                json={"result": {"note": "办完了"}}, headers=h)
    again = client.post(f"/api/spd/tasks/{task['id']}/complete",
                        json={"result": {"note": "再办一次"}}, headers=h)
    assert again.status_code == 409


# ============================================================ 批量与导出


def test_批量认领与批量取消(client, h, base):
    tasks = [_task(client, h, base, title=f"批量{i}")["id"] for i in range(3)]
    claimed = client.post("/api/spd/tasks/batch",
                          json={"task_ids": tasks, "action": "claim"}, headers=h).json()
    assert claimed["processed"] == 3 and claimed["skipped"] == [], claimed
    # skipped 是**逐条带原因**的列表而不是一个计数——批量操作最怕"报了成功、
    # 实际跳过了几条"，只给数字的话没人知道跳的是哪几条、为什么

    cancelled = client.post(
        "/api/spd/tasks/batch",
        json={"task_ids": tasks, "action": "cancel", "note": "方案调整，统一撤"},
        headers=h,
    ).json()
    assert cancelled["processed"] == 3
    for task_id in tasks:
        assert client.get(f"/api/spd/tasks/{task_id}", headers=h).json()["status"] == "cancelled"


def test_批量指派要求给出责任人(client, h, base):
    task = _task(client, h, base, title="批量指派")
    resp = client.post("/api/spd/tasks/batch",
                       json={"task_ids": [task["id"]], "action": "assign"}, headers=h)
    assert resp.status_code == 422, "批量指派没给责任人，应当拒绝而不是指给空"


def test_任务导出带表头且能按状态筛(client, h, base):
    """导出返回"表头 + 行"的 JSON，由前端拼 CSV（与平台既有导出同一形状）。"""
    _task(client, h, base, title="待导出任务")
    resp = client.get("/api/spd/tasks-export?status=pending", headers=h)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["columns"], "没有表头，拿到手不知道哪列是什么"
    assert payload["rows"], "按 pending 筛应当有数据"
    assert len(payload["rows"][0]) == len(payload["columns"]), "行宽必须与表头一致"


def test_任务汇总按类型与状态分组(client, h, base):
    summary = client.get(f"/api/spd/tasks/summary?org_id={base['org']['id']}",
                         headers=h).json()
    assert summary["open_total"] >= 1
    assert isinstance(summary["by_status"], dict) and summary["by_status"]
    assert isinstance(summary["open_by_type"], dict)
    assert "due_today" in summary and "escalated" in summary


# ============================================================ 护理服务侧


def test_干预计划从模板开出并可完成(client, h, base):
    template = client.post(
        "/api/spd/intervention-templates",
        json={"code": "itpl_diet", "name": "低盐饮食干预", "program_code": "hypertension",
              "category": "diet", "content": "每日食盐<5g", "frequency": "每周",
              "cycle_days": 30},
        headers=h,
    ).json()
    created = client.post(
        "/api/spd/interventions",
        json={"patient_ids": [base["patient"]["id"]], "template_id": template["id"],
              "program_code": "hypertension", "goal": "血压达标"},
        headers=h,
    )
    assert created.status_code == 201, created.text
    intervention = client.get(
        f"/api/spd/interventions?patient_id={base['patient']['id']}", headers=h
    ).json()[0]
    done = client.patch(f"/api/spd/interventions/{intervention['id']}",
                        json={"status": "done", "feedback": "血压降至 132/84"},
                        headers=h).json()
    assert done["status"] == "done" and "132/84" in done["feedback"]


def test_复诊改期与完成都留在日志里(client, h, base):
    from datetime import date, timedelta

    plan_date = (date.today() + timedelta(days=7)).isoformat()
    revisit = client.post(
        "/api/spd/revisits",
        json={"patient_id": base["patient"]["id"], "plan_date": plan_date,
              "items": "复查血压", "source": "manual"},
        headers=h,
    ).json()
    changed = client.patch(
        f"/api/spd/revisits/{revisit['id']}",
        json={"plan_date": (date.today() + timedelta(days=14)).isoformat(),
              "note": "患者外出，改期一周"},
        headers=h,
    ).json()
    assert changed["plan_date"] != plan_date
    assert changed["log"], "改期要留日志，否则事后说不清是谁改的"

    finished = client.patch(f"/api/spd/revisits/{revisit['id']}",
                            json={"status": "done", "actual_date": date.today().isoformat()},
                            headers=h).json()
    assert finished["status"] == "done" and finished["actual_date"]


def test_个案上报到处置闭环(client, h, base):
    task = client.post(
        "/api/spd/case-report-tasks",
        json={"code": "crt_bp", "name": "血压危急值上报", "program_code": "hypertension",
              "dept": "全科"},
        headers=h,
    ).json()
    report = client.post(
        "/api/spd/case-reports",
        json={"task_id": task["id"], "patient_id": base["patient"]["id"],
              "report_type": "dispose", "content": "血压 190/110，村医上报"},
        headers=h,
    )
    assert report.status_code == 201, report.text
    report = report.json()
    handled = client.post(
        f"/api/spd/case-reports/{report['id']}/handle",
        json={"status": "done", "handle_note": "已联系急救"},
        headers=h,
    ).json()
    assert handled["status"] in ("done", "closed")
    listed = client.get(f"/api/spd/case-reports?task_id={task['id']}", headers=h).json()
    assert listed and listed[0]["status"] in ("done", "closed")
