"""慢专病任务被审核人退回后，办理人点「办结」就直接完成：不用重提、不再过审，随访日照样回写、计分照记（P2-1361，
第四十批「审核与审批」扫描 AD2-1）。

审核接口的 docstring 写着「退回置『已退回』，回到办理人手里按审核意见重新提交」，`service.TASK_COMPLETABLE_STATUSES`
的注释写着「通过即办结、退回即重办」；P2-244 已经挡了「待审核的直接办结」。可办结接口的前置判定与条件翻转都按
`TASK_COMPLETABLE_STATUSES`（未结束的除了待审核）放行，**含已退回**：村医被退回的随访点一下办结就成了已完成，
`last_followup_at` 记成今天、下次随访往后推一个周期、计分照记，审核意见原样挂在一条「已完成」的任务上。两端页面也
给了这个口子：管理端任务中心的退回行「提交」「办结」并排，医生移动端的退回卡片上只有「办结」、没有重新提交的入口。

修法：另起「能直接办结」的集合 `service.TASK_DIRECT_COMPLETE_STATUSES`（再去掉已退回），办结接口的前置判定与条件
UPDATE 都按它，已退回的 409「已被审核人退回，请按审核意见重新提交审核」；`TASK_COMPLETABLE_STATUSES` 不动——重新提交、
补佐证、居民端提交都靠它收已退回的。管理端退回行不摆「办结」，医生移动端退回卡片的「办结」换成「重新提交」（与管理端
「提交」同一个接口、同一份取数）。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import login

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "server" / "app" / "static"
ADMIN_PAGE = (STATIC / "pages-spd.js").read_text(encoding="utf-8")
MOBILE = (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")
MANUAL = (ROOT / "docs" / "培训手册" / "村医手册.md").read_text(encoding="utf-8")
B = "/api/spd"
LAST, NEXT = "2026-07-01", "2026-10-01"
_seq = {"n": 0}


@pytest.fixture(scope="module")
def world(client, admin):
    county = client.post("/api/organizations", headers=admin, json={
        "name": "P21361 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21361 卫生院", "org_type": "township", "level": "township", "parent_id": county}).json()["id"]
    users = {}
    for name, full in (("vd", "P21361 村医"), ("reviewer", "P21361 卫生院医生")):
        resp = client.post("/api/users", headers=admin, json={
            "username": f"p21361_{name}", "password": "pw123456", "full_name": full, "role": "doctor", "org_id": org})
        assert resp.status_code in (200, 201), resp.text
        users[name] = resp.json()["id"]
    heads = {name: login(client, f"p21361_{name}", "pw123456") for name in users}
    return {"org": org, "users": users, "heads": heads}


def _followup_task(client, admin, world):
    """一位在管患者（村医签约、上次随访 07-01、下次 10-01）名下一条村医已接收的随访任务。返回 (任务 id, 档案 id)。"""
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment, SpdTask

    _seq["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P21361 患者{_seq['n']}", "id_card": f"33010619550101{_seq['n']:04d}"})
    assert patient.status_code == 201, patient.text
    with SessionLocal() as db:
        enrollment = SpdEnrollment(patient_id=patient.json()["id"], program_code="hypertension", org_id=world["org"],
                                   status="active", village_doctor_id=world["users"]["vd"],
                                   last_followup_at=LAST, next_followup_at=NEXT)
        db.add(enrollment)
        db.flush()
        task = SpdTask(patient_id=patient.json()["id"], enrollment_id=enrollment.id, org_id=world["org"],
                       program_code="hypertension", task_type="followup", title="P21361 高血压季度随访",
                       status="claimed", assignee_id=world["users"]["vd"])
        db.add(task)
        db.commit()
        return task.id, enrollment.id


def _state(task_id, enrollment_id):
    """（任务状态, 审核意见, 上次随访, 下次随访, 这条任务记了几笔随访分）"""
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment, SpdPointRecord, SpdTask

    with SessionLocal() as db:
        task, enrollment = db.get(SpdTask, task_id), db.get(SpdEnrollment, enrollment_id)
        points = db.query(SpdPointRecord).filter_by(ref_type="task", ref_id=task_id, direction="in").count()
        return task.status, task.review_note, enrollment.last_followup_at, enrollment.next_followup_at, points


def _submit_and_reject(client, world, task_id, note="血压未控制、未核实服药，请复测并核实后重新提交"):
    vd, reviewer = world["heads"]["vd"], world["heads"]["reviewer"]
    submitted = client.post(f"{B}/tasks/{task_id}/submit", headers=vd, json={"result": {"note": "入户随访，血压 168/102"}})
    assert submitted.status_code == 200 and submitted.json()["status"] == "submitted", submitted.text
    rejected = client.post(f"{B}/tasks/{task_id}/review", headers=reviewer, json={"approved": False, "note": note})
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected", rejected.text
    return note


def test_退回后直接办结_409_随访日与计分都不动(client, admin, world):
    from app.spd.routers.tasks import RETURNED_FOR_RESUBMIT

    task, enrollment = _followup_task(client, admin, world)
    note = _submit_and_reject(client, world, task)
    resp = client.post(f"{B}/tasks/{task}/complete", headers=world["heads"]["vd"], json={"result": {"note": "已办"}})
    # 修前 200：('done', 退回意见, 今天, 今天 + 90 天, 1)——退回意见挂在一条已完成的任务上
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == RETURNED_FOR_RESUBMIT
    assert _state(task, enrollment) == ("rejected", note, LAST, NEXT, 0)


def test_按意见重新提交_再审通过才办结_只计一次分(client, admin, world):
    from conftest import business_today_str

    task, enrollment = _followup_task(client, admin, world)
    _submit_and_reject(client, world, task)
    vd, reviewer = world["heads"]["vd"], world["heads"]["reviewer"]
    again = client.post(f"{B}/tasks/{task}/submit", headers=vd, json={"result": {"note": "已复测 132/80，已核实服药"}})
    assert again.status_code == 200 and again.json()["status"] == "submitted", again.text
    assert _state(task, enrollment)[2:] == (LAST, NEXT, 0)   # 重提只是回到待审核，还没办结
    passed = client.post(f"{B}/tasks/{task}/review", headers=reviewer, json={"approved": True, "note": "同意"})
    assert passed.status_code == 200 and passed.json()["status"] == "done", passed.text
    status, _note, last, following, points = _state(task, enrollment)
    assert (status, last, points) == ("done", business_today_str(), 1)
    assert following > last   # 下次随访按周期往后排了
    # 已办结的再点办结 / 再审一次都是 409，分不再记
    assert client.post(f"{B}/tasks/{task}/complete", headers=vd, json={}).status_code == 409
    assert client.post(f"{B}/tasks/{task}/review", headers=reviewer, json={"approved": True}).status_code == 409
    assert _state(task, enrollment)[4] == 1


def test_不走审核的任务照旧直接办结(client, admin, world):
    from conftest import business_today_str

    task, enrollment = _followup_task(client, admin, world)
    resp = client.post(f"{B}/tasks/{task}/complete", headers=world["heads"]["vd"], json={"result": {"note": "已上门"}})
    assert resp.status_code == 200 and resp.json()["status"] == "done", resp.text
    status, _note, last, _following, points = _state(task, enrollment)
    assert (status, last, points) == ("done", business_today_str(), 1)


def test_锁外读到已接收_这时已被审核人退回_办结同样不绕过再审(client, admin, world, monkeypatch):
    """前置判定之后、条件翻转之前，办理人那边提交、审核人退回了：条件 UPDATE 也不从「已退回」翻（与 P2-244 同一处）。"""
    from app.database import SessionLocal
    from app.spd.models import SpdTask
    from app.spd.routers import tasks as tasks_router
    from app.spd.routers.tasks import RETURNED_FOR_RESUBMIT

    task, enrollment = _followup_task(client, admin, world)
    real_now = tasks_router.now_naive
    fired = []

    def rejected_meanwhile():
        if not fired:
            fired.append(True)
            with SessionLocal() as other:
                row = other.get(SpdTask, task)
                row.status, row.review_note = "rejected", "请复测"
                other.commit()
        return real_now()

    monkeypatch.setattr(tasks_router, "now_naive", rejected_meanwhile)
    resp = client.post(f"{B}/tasks/{task}/complete", headers=world["heads"]["vd"], json={})
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200：刚被退回的照样办结、计分
    assert resp.json()["detail"] == RETURNED_FOR_RESUBMIT
    assert _state(task, enrollment) == ("rejected", "请复测", LAST, NEXT, 0)


# ------------------------------------------------------------ 两端页面


def _function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    return src[start:src.index("\n}\n", start) + 2]


def _run(src: str, name: str, rows: list[dict]) -> list[str]:
    script = (_function(src, name)
              + f"\nconsole.log(JSON.stringify(JSON.parse(process.argv[1]).map((t) => {name}(t))));")
    out = subprocess.run(["node", "-e", script, json.dumps(rows)], capture_output=True, text=True, check=True,
                         timeout=60).stdout
    return json.loads(out)


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端函数")
def test_管理端任务中心_退回行有提交没有办结():
    rows = [{"id": 1, "status": "rejected", "assignee_id": 7, "escalated": False, "claimable": False},
            {"id": 2, "status": "doing", "assignee_id": 7, "escalated": False, "claimable": False},
            {"id": 3, "status": "submitted", "assignee_id": 7}]
    rejected, doing, submitted = _run(ADMIN_PAGE, "spdTaskActions", rows)
    assert 'data-task-submit="1"' in rejected and 'data-task-done="1"' not in rejected   # 修前「提交」「办结」并排
    assert 'data-task-done="2"' in doing   # 不走审核的照旧能直接办结
    assert 'data-task-review="3"' in submitted and 'data-task-done="3"' not in submitted


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端函数")
def test_医生移动端_退回卡片换成重新提交():
    rows = [{"id": 1, "status": "rejected", "require_evidence": True},
            {"id": 2, "status": "claimed", "require_evidence": False},
            {"id": 3, "status": "submitted"}]
    rejected, claimed, submitted = _run(MOBILE, "spdTodoOps", rows)
    assert 'data-spd-resubmit="1"' in rejected and "重新提交" in rejected   # 修前没有重新提交的入口
    assert 'data-spd-done="1"' not in rejected   # 修前只有「办结」
    assert 'data-spd-evidence="1"' in rejected   # 要佐证的照样能补传
    assert 'data-spd-done="2"' in claimed and "data-spd-resubmit" not in claimed
    assert submitted == ""


def test_医生移动端的重新提交走提交接口_取数与管理端提交一致():
    start = MOBILE.index('box.querySelectorAll("[data-spd-resubmit]")')
    handler = MOBILE[start:MOBILE.index("\n}\n", start)]
    assert "/submit`" in handler and "/complete`" not in handler
    assert "{ result: { note: f.note.value.trim() } }" in handler   # 同管理端「提交」：办理结果写进 result.note
    assert "draft" not in handler   # 不给存草稿：存了就翻成办理中，卡片上又摆回「办结」
    at = ADMIN_PAGE.index("if (submit) {")
    admin_submit = ADMIN_PAGE[at:ADMIN_PAGE.index("if (review) {", at)]
    assert "/submit`" in admin_submit and "result: { note: form.note" in admin_submit   # 判据自证：管理端「提交」的取数


def test_村医手册写了退回的任务重新提交():
    section = MANUAL[MANUAL.index("## 二、办任务"):MANUAL.index("## 三、")]
    assert "**退回**" in section and "**重新提交**" in section and "没有「办结」" in section
