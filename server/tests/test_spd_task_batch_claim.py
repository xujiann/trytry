"""批量「接收」的前置状态比单条宽：自己提交在等审核的任务，批量一勾就被接收回「已接收」、拉出审核队列（P2-83）。

单条接收（`claim_task`）只收待接收与已超期的任务，其余 409「该任务不处于可接收状态」；批量接收的条件却是「未结束、
且没被别人接」——自己提交在等审核的、办理中的、被退回待重办的，批量一勾都成了「已接收」。待审核的那条从此不在审核
队列里（审核只认待审核），审核人点审核 409，得办理人重新提交一遍；退回的那条丢了「已退回」的标记。

修法：批量接收与单条同一前置状态（`service.TASK_CLAIMABLE_STATUSES`），不满足的进 skipped，原因按「已结束 / 已被
他人接收 / 不处于可接收状态」给出。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    from conftest import login

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P283 批量接收卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P283 患者", "id_card": "330127196901010283"}).json()["id"]
    resp = client.post("/api/users", headers=admin, json={
        "username": "p283_doc", "password": "passw0rd1", "role": "doctor", "full_name": "P283 医生", "org_id": org})
    assert resp.status_code == 201, resp.text
    return {"org": org, "patient": patient, "doctor": login(client, "p283_doc", "passw0rd1")}


def _task(client, admin, world, title):
    resp = client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": world["patient"], "title": title, "task_type": "followup", "org_id": world["org"]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _batch_claim(client, headers, task_ids):
    resp = client.post(f"{B}/tasks/batch", headers=headers, json={"task_ids": task_ids, "action": "claim"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _status(client, admin, task_id):
    return client.get(f"{B}/tasks/{task_id}", headers=admin).json()["status"]


def test_自己提交待审核的任务_批量接收跳过_照旧能审(client, admin, world):
    task = _task(client, admin, world, "P283 待审核")
    assert client.post(f"{B}/tasks/{task}/submit", headers=admin, json={"result": {"note": "已上门"}}).status_code == 200
    body = _batch_claim(client, admin, [task])
    assert body == {"processed": 0, "skipped": [{"id": task, "reason": "不处于可接收状态"}]}   # 修前 processed 1
    assert _status(client, admin, task) == "submitted"   # 修前被接收回 claimed，拉出审核队列
    review = client.post(f"{B}/tasks/{task}/review", headers=admin, json={"approved": True})
    assert review.status_code == 200 and review.json()["status"] == "done", review.text   # 修前 409


def test_批量接收与单条同一口径(client, admin, world):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    pending, overdue, doing, rejected, done, others = (
        _task(client, admin, world, f"P283 {name}") for name in ("待接收", "已超期", "办理中", "已退回", "已完成", "别人的"))
    with SessionLocal() as db:
        db.get(SpdTask, overdue).status = "overdue"
        db.commit()
    client.post(f"{B}/tasks/{doing}/submit", headers=admin, json={"result": {}, "draft": True})
    client.post(f"{B}/tasks/{rejected}/submit", headers=admin, json={"result": {}})
    client.post(f"{B}/tasks/{rejected}/review", headers=admin, json={"approved": False, "note": "重办"})
    client.post(f"{B}/tasks/{done}/complete", headers=admin, json={"result": {"note": "办完"}})
    assert client.post(f"{B}/tasks/{others}/claim", headers=world["doctor"]).status_code == 200
    assert [_status(client, admin, t) for t in (pending, overdue, doing, rejected, done, others)] == [
        "pending", "overdue", "doing", "rejected", "done", "claimed"]

    body = _batch_claim(client, admin, [pending, overdue, doing, rejected, done, others])
    body["skipped"].sort(key=lambda s: s["id"])
    assert body == {"processed": 2, "skipped": [
        {"id": doing, "reason": "不处于可接收状态"},      # 修前 processed：办理中退回「已接收」
        {"id": rejected, "reason": "不处于可接收状态"},   # 修前 processed：丢了「已退回」
        {"id": done, "reason": "任务已结束"},
        {"id": others, "reason": "已被他人接收"},
    ]}, body
    assert [_status(client, admin, t) for t in (pending, overdue, doing, rejected)] == [
        "claimed", "claimed", "doing", "rejected"]
    # 单条接收对同样四种不可接收的给 409
    for task in (doing, rejected, done, others):
        assert client.post(f"{B}/tasks/{task}/claim", headers=admin).status_code == 409
