"""退回（rejected）的慢专病任务被当成「已结束」：路径越过它往下走、待办和计数里看不到它、结案不收它（P1-127）。

审核「退回」把任务置为 `rejected`。审核接口的 docstring 写的是「退回则回到办理中」，提交 / 办结接口也照收它——
它是**没办完**的任务。可「未结束」这个集合在七处各写了一份（任务路由与工作台各一份 `OPEN_STATUSES`、结案取消、
超期扫描、居民端首页待办数、报告的待办数与待办表），七份都没有 `rejected`：

- 同节点两条任务，一条被退回待重办、另一条办完——路径照样推进到下一节点，退回的那条重办完也推不回来；
  手工推进同样不拦；
- 「我的待办」（医生移动端就是按它取的）、待办统计、工作台、报告里的待办数与待办表都不数它——被退回的人
  在待办里看不到它；
- 患者死亡 / 迁出 / 排除时的结案取消、路径取消都不收它：死者名下还挂着一条能办结、能给村医计分的任务；
- 催办 / 升级 409「该任务已结束」（任务中心页上这两个按钮却给着），批量处理一律跳过；
- 过了截止日也不超期；
- 居民端：退回给居民重做的任务，首页待办数不数它。

修法：`service.TASK_OPEN_STATUSES`（未结束）与 `TASK_IN_HAND_STATUSES`（在办理人手里、未提交未超期）一处定义、
各处引用，两者都含 `rejected`；闸门 `test_spd_task_status_sets.py` 不许再在查询里手写状态清单。
"""
import pytest

B = "/api/spd"
_seq = {"n": 0}


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdPathNode, SpdPathTemplate, SpdProgram

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P127 退回任务卫生院", "org_type": "township", "level": "township"}).json()["id"]
    with SessionLocal() as db:
        program = db.query(SpdProgram).filter_by(code="hypertension").one()
        template = SpdPathTemplate(program_id=program.id, code="P127_PATH", name="P127 两节点路径",
                                   status="published")
        db.add(template)
        db.flush()
        db.add_all([SpdPathNode(template_id=template.id, key="n1", name="首诊", seq=1),
                    SpdPathNode(template_id=template.id, key="n2", name="复诊", seq=2)])
        db.commit()
        template_id = template.id
    return {"org": org, "template": template_id}


def _enrolled(client, admin, world, phone=None):
    """新开一位在管患者（结案那条会把档案关掉，各用例各开一位）。返回 (患者 id, 纳管档案 id)。"""
    _seq["n"] += 1
    body = {"name": f"P127 患者{_seq['n']}", "id_card": f"33012719680303{_seq['n']:04d}"}
    if phone:
        body["phone"] = phone
    patient = client.post("/api/patients", headers=admin, json=body)
    assert patient.status_code == 201, patient.text
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient.json()["id"], "program_code": "hypertension", "org_id": world["org"]})
    assert enrollment.status_code == 201, enrollment.text
    return patient.json()["id"], enrollment.json()["id"]


def _task(client, admin, world, patient, enrollment, title):
    resp = client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": patient, "enrollment_id": enrollment, "title": title, "task_type": "followup",
        "org_id": world["org"]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _submit(client, admin, task_id):
    resp = client.post(f"{B}/tasks/{task_id}/submit", headers=admin, json={"result": {"note": "已上门"}})
    assert resp.status_code == 200 and resp.json()["status"] == "submitted", resp.text


def _reject(client, admin, task_id):
    """审核退回：退回的任务回到办理人手里重办。"""
    resp = client.post(f"{B}/tasks/{task_id}/review", headers=admin, json={"approved": False, "note": "缺血压记录"})
    assert resp.status_code == 200 and resp.json()["status"] == "rejected", resp.text


def _path(world, patient, enrollment, n_tasks):
    """一个停在 n1 的执行中路径实例，n1 上挂 n_tasks 条待办。返回 (实例 id, 任务 id 列表)。"""
    from app.database import SessionLocal
    from app.spd.models import SpdPathInstance, SpdTask

    with SessionLocal() as db:
        instance = SpdPathInstance(enrollment_id=enrollment, template_id=world["template"], status="running",
                                   current_node_key="n1")
        db.add(instance)
        db.flush()
        tasks = [SpdTask(patient_id=patient, enrollment_id=enrollment, instance_id=instance.id, node_key="n1",
                         task_type="path", title=f"P127 首诊任务{i}", org_id=world["org"],
                         program_code="hypertension")
                 for i in range(n_tasks)]
        db.add_all(tasks)
        db.commit()
        return instance.id, [t.id for t in tasks]


def test_同节点一条退回待重办_另一条办完_路径不越过它(client, admin, world):
    patient, enrollment = _enrolled(client, admin, world)
    instance, (returned, other) = _path(world, patient, enrollment, 2)
    _submit(client, admin, returned)
    _reject(client, admin, returned)
    done = client.post(f"{B}/tasks/{other}/complete", headers=admin, json={"result": {"note": "办完"}})
    assert done.status_code == 200 and "advanced" not in done.json(), done.text   # 修前推进到 n2
    assert client.get(f"{B}/path-instances/{instance}", headers=admin).json()["current_node_key"] == "n1"
    # 手工推进同样要拦：当前节点还有退回待重办的任务
    resp = client.post(f"{B}/path-instances/{instance}/advance", headers=admin)
    assert resp.status_code == 409 and resp.json() == {"detail": "当前节点仍有未完成任务，不能推进"}, resp.text
    # 退回的那条重办完，路径才往下走
    redo = client.post(f"{B}/tasks/{returned}/complete", headers=admin, json={"result": {"note": "补了血压"}})
    assert redo.status_code == 200 and redo.json()["advanced"]["current_node_key"] == "n2", redo.text


def test_退回的任务还在我的待办里_各处待办数都数它(client, admin, world):
    from app.database import SessionLocal
    from app.spd.reporting import compose_section

    patient, enrollment = _enrolled(client, admin, world)
    task = _task(client, admin, world, patient, enrollment, "P127 待办计数")
    _submit(client, admin, task)   # 提交即挂到提交人名下；待审核本就算未结束

    def counts():
        with SessionLocal() as db:
            report = compose_section(db, {"key": "summary"}, world["org"], "daily")["metrics"]["open_tasks"]
        return (client.get(f"{B}/tasks/summary?org_id={world['org']}", headers=admin).json()["open_total"],
                client.get(f"{B}/workbench/doctor-mobile", headers=admin).json()["todo"]["open"],
                report)

    before = counts()
    _reject(client, admin, task)
    assert counts() == before   # 修前三个数各少 1
    mine = client.get(f"{B}/tasks?mine=true&open_only=true&limit=500", headers=admin).json()
    assert task in [t["id"] for t in mine]   # 修前不在：医生移动端「我的待办」就是这么取的
    with SessionLocal() as db:
        todo = compose_section(db, {"key": "todo"}, world["org"], "daily")
    assert "P127 待办计数" in [row[0] for row in todo["rows"]]   # 报告的待办表，修前不列


def test_退回的任务能催办能升级_批量处理不跳过(client, admin, world):
    patient, enrollment = _enrolled(client, admin, world)
    task = _task(client, admin, world, patient, enrollment, "P127 催办")
    _submit(client, admin, task)
    _reject(client, admin, task)
    urge = client.post(f"{B}/tasks/{task}/urge", headers=admin)
    assert urge.status_code == 200 and urge.json()["urged_count"] == 1, urge.text   # 修前 409「该任务已结束」
    escalate = client.post(f"{B}/tasks/{task}/escalate", headers=admin)
    assert escalate.status_code == 200 and escalate.json()["escalated"] is True, escalate.text
    batch = client.post(f"{B}/tasks/batch", headers=admin, json={"task_ids": [task], "action": "urge"})
    assert batch.status_code == 200 and batch.json() == {"processed": 1, "skipped": []}, batch.text


def test_患者死亡结案_退回的任务一并取消(client, admin, world):
    patient, enrollment = _enrolled(client, admin, world)
    task = _task(client, admin, world, patient, enrollment, "P127 结案")
    _submit(client, admin, task)
    _reject(client, admin, task)
    resp = client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=admin,
                       json={"event": "death", "reason": "病故"})
    assert resp.status_code == 200 and resp.json()["closed"]["tasks"] == 1, resp.text   # 修前 0
    detail = client.get(f"{B}/tasks/{task}", headers=admin).json()
    # 修前仍是 rejected：死者名下挂着一条还能办结、还能给村医计分的任务
    assert (detail["status"], detail["review_note"]) == ("cancelled", "death:病故"), detail


def test_路径取消_退回的任务一并取消(client, admin, world):
    patient, enrollment = _enrolled(client, admin, world)
    instance, (task,) = _path(world, patient, enrollment, 1)
    _submit(client, admin, task)
    _reject(client, admin, task)
    resp = client.patch(f"{B}/path-instances/{instance}", headers=admin, json={"status": "cancelled"})
    assert resp.status_code == 200, resp.text
    detail = client.get(f"{B}/tasks/{task}", headers=admin).json()
    assert (detail["status"], detail["review_note"]) == ("cancelled", "路径取消"), detail   # 修前仍是 rejected


def test_退回的任务过了截止日照样超期(client, admin, world):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    patient, enrollment = _enrolled(client, admin, world)
    task = _task(client, admin, world, patient, enrollment, "P127 超期")
    _submit(client, admin, task)
    _reject(client, admin, task)
    with SessionLocal() as db:
        db.get(SpdTask, task).due_date = "2020-01-01"
        db.commit()
    client.get(f"{B}/tasks/summary", headers=admin)   # 进工作台顺手扫一次超期（同定时任务）
    detail = client.get(f"{B}/tasks/{task}", headers=admin).json()
    assert (detail["status"], detail["review_note"]) == ("overdue", "缺血压记录"), detail   # 修前仍是 rejected


def test_居民端首页待办数_数退回给居民重做的任务(client, admin, world):
    from app.config import settings
    from app.database import SessionLocal
    from app.models import ResidentAccount

    phone = "13912701127"
    patient, enrollment = _enrolled(client, admin, world, phone=phone)
    with SessionLocal() as db:
        db.add(ResidentAccount(phone=phone, patient_id=patient, nickname="P127", wechat_openid="", status="active"))
        db.commit()
    old = settings.sms_debug_echo
    settings.sms_debug_echo = True
    try:
        code = client.post("/api/portal/auth/sms/code", json={"phone": phone, "purpose": "login"}).json()["debug_code"]
        token = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code}).json()["access_token"]
    finally:
        settings.sms_debug_echo = old
    resident = {"Authorization": f"Bearer {token}"}
    task = _task(client, admin, world, patient, enrollment, "P127 居民重做")
    resp = client.post(f"/api/portal/spd/tasks/{task}/submit", headers=resident, json={"result": {"note": "已测"}})
    assert resp.status_code == 200, resp.text
    assert client.get("/api/portal/spd/home", headers=resident).json()["todo"]["tasks"] == 0   # 待审核：等医护，不算居民的待办
    _reject(client, admin, task)
    assert client.get("/api/portal/spd/home", headers=resident).json()["todo"]["tasks"] == 1   # 修前 0
