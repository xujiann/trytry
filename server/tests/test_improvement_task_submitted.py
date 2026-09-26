"""绩效整改任务提交完成、待确认之后，不能再用「登记进展」悄悄改回整改中（P2-192）。

状态流转：待整改 → 整改中（登记措施）→ 已提交完成（待确认）→ 已确认关闭 / 退回整改中。页面上待确认的只给「确认关闭 /
退回」，退回走确认接口、记下退回人与理由；登记进展接口却只拦「已确认关闭」——对待确认的任务不带 complete 调一次，
状态改回「整改中」、完成时间还留着，任务悄悄退出管理层的待确认队列。
"""
import pytest


@pytest.fixture(scope="module")
def task(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2192 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    created = client.post("/api/performance/improvements", headers=admin, json={
        "org_id": org, "problem": "P2192 随访率偏低", "owner_name": "张三", "due_date": "2030-01-01"})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_待确认的任务登记进展是409_状态不动(client, admin, task):
    done = client.post(f"/api/performance/improvements/{task}/progress", headers=admin,
                       json={"complete": True, "completion_note": "已补做随访 30 人"})
    assert done.status_code == 200 and done.json()["status"] == "completed", done.text
    resp = client.post(f"/api/performance/improvements/{task}/progress", headers=admin, json={"measures": "再补 5 人"})
    assert resp.status_code == 409, resp.text   # 修前 200、状态改回 in_progress
    row = next(t for t in client.get("/api/performance/improvements", headers=admin).json() if t["id"] == task)
    assert row["status"] == "completed"


def test_退回整改之后照常登记进展(client, admin, task):
    back = client.post(f"/api/performance/improvements/{task}/verify", headers=admin,
                       json={"approve": False, "comment": "随访记录不全"})
    assert back.status_code == 200 and back.json()["status"] == "in_progress", back.text
    resp = client.post(f"/api/performance/improvements/{task}/progress", headers=admin, json={"measures": "补全记录"})
    assert resp.status_code == 200 and resp.json()["status"] == "in_progress", resp.text
