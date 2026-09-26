"""查询接口的 `?today=` 覆盖驱动了一次全县、永久的超期扫描：任一账号带个未来日期，别家在办的任务全被打成超期（P0-47）。

五个查询入口进门顺手跑 `sweep_overdue`（待办统计、平台管理端 / 中心端工作台、`overdue=true` 的随访与复诊看板），
扫描的截止日取自 `resolve_business_date(today)`——`today` 覆盖参数按接口对接规范「仅限测试与管理排查用途」，
可它不设限，扫描也不分机构，结果是永久的：置超期、按节点配置升级并通知上级，考核按状态取数。修前实测：与谁都没关系的
卫生院医生 `GET /api/spd/tasks/summary?today=2099-12-31`，别家机构三天后才到期的任务当场变成「超期」。

修法：查询照旧按覆盖日期算（「今天到期」之类），写库不跟它往后拨——扫描截止日取覆盖日期与真实今天里早的那个
（`service.sweep_overdue_on_read`）。往回看的覆盖照用：早于今天的截止日扫得到的，今天也一定扫得到。
"""
from datetime import timedelta

import pytest
from conftest import freeze_business_date

from app import clock
from app.database import SessionLocal

B = "/api/spd"
FAR = "2099-12-31"


def _login(client, username, password="pw123456"):
    token = client.post("/api/auth/login", json={"username": username, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    from app.spd.models import SpdTask

    a = client.post("/api/organizations", headers=admin, json={
        "name": "P0047 纳管卫生院", "org_type": "township", "level": "township"}).json()["id"]
    b = client.post("/api/organizations", headers=admin, json={
        "name": "P0047 无关卫生院", "org_type": "township", "level": "township"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p0047_doc_b", "password": "pw123456", "full_name": "p0047_doc_b", "role": "doctor", "org_id": b})
    assert created.status_code == 201, created.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P0047 患者", "id_card": "330127197309090047"}).json()["id"]
    due = (clock.today() + timedelta(days=3)).isoformat()
    with SessionLocal() as db:
        task = SpdTask(patient_id=patient, title="P0047 三天后到期的随访", task_type="followup", org_id=a,
                       status="pending", due_date=due)
        db.add(task)
        db.commit()
        task_id = task.id
    return {"task": task_id, "due": due, "doc_b": _login(client, "p0047_doc_b")}


def _status(task_id):
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        return db.get(SpdTask, task_id).status


def _reset(task_id):
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        db.get(SpdTask, task_id).status = "pending"
        db.commit()


@pytest.mark.parametrize("path,params", [
    ("/tasks/summary", {}),
    ("/workbench/center", {}),
    ("/followup-records", {"overdue": "true"}),
    ("/revisits", {"overdue": "true"}),
], ids=["待办统计", "中心端工作台", "随访看板", "复诊看板"])
def test_无关机构带未来日期查一次_别家没到期的任务不被打成超期(client, world, path, params):
    _reset(world["task"])
    got = client.get(f"{B}{path}", params={**params, "today": FAR}, headers=world["doc_b"])
    assert got.status_code == 200, got.text
    assert _status(world["task"]) == "pending"   # 修前 overdue：三天后才到期的任务被永久改了状态


def test_平台管理端工作台同样不往后拨(client, world, admin):
    _reset(world["task"])
    got = client.get(f"{B}/workbench/admin", params={"today": FAR}, headers=admin)
    assert got.status_code == 200, got.text
    assert _status(world["task"]) == "pending"   # 修前 overdue


def test_查询照旧按覆盖日期算(client, world, admin):
    _reset(world["task"])
    summary = client.get(f"{B}/tasks/summary", params={"today": world["due"]}, headers=admin)
    assert summary.status_code == 200, summary.text
    assert summary.json()["due_today"] >= 1   # 「今天到期」按覆盖日期数，排查用途不受影响
    assert _status(world["task"]) == "pending"


def test_真到了期_进页面照常刷新成超期(client, world, admin):
    """与改前的 `test_spd_flow::test_overdue_sweep_marks_tasks` 同一个窗口（今天到期、冻到明天），只是改用冻结业务日期拨日子。"""
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        original = db.get(SpdTask, world["task"])
        task = SpdTask(patient_id=original.patient_id, title="P0047 今天到期的随访", task_type="followup",
                       org_id=original.org_id, status="pending", due_date=clock.today().isoformat())
        db.add(task)
        db.commit()
        task_id = task.id
    with freeze_business_date(clock.today() + timedelta(days=1)):
        summary = client.get(f"{B}/tasks/summary", headers=admin)
        assert summary.status_code == 200, summary.text
        assert summary.json()["swept"]["overdue"] >= 1
    assert _status(task_id) == "overdue"
