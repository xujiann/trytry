"""报告推送任务 PATCH status=deleted 之后：清单不再列、不能改回来、不能再手工出报告（P2-832，第二十二批「删除与引用完整性」
扫描 X2-4）。

`update_report_task` 的说明写「删除也走这里（status=deleted 由前端不再展示）」；清单不带 status 时却全列，页面把非启用的一律
显示成「暂停」并给「启用」「立即执行」——经接口删掉的任务在页面上是一条暂停任务，点「启用」就复活，点「立即执行」照样
出报告。定时推送本就只跑启用的（`spd/jobs.py`）。修后清单默认不列已删除的，已删除的改不了（409）、手工生成 409。
"""
B = "/api/spd"


def _task(client, admin, name):
    templates = client.get(f"{B}/report-templates", headers=admin).json()
    if not templates:
        section = client.get(f"{B}/meta", headers=admin).json()["report_sections"][0]
        made = client.post(f"{B}/report-templates", headers=admin, json={
            "code": "P2832T", "name": "P2832 周报", "period": "weekly",
            "sections": [{"key": section["key"], "title": section["name"]}]})
        assert made.status_code == 201, made.text
        templates = [made.json()]
    task = client.post(f"{B}/report-tasks", headers=admin, json={"template_id": templates[0]["id"], "name": name})
    assert task.status_code == 201, task.text
    return task.json()["id"]


def test_删掉的推送任务_不列_不能复活_不能再出报告(client, admin):
    task_id = _task(client, admin, "P2832 每日推送")
    gone = client.patch(f"{B}/report-tasks/{task_id}", headers=admin, json={"status": "deleted"})
    assert gone.status_code == 200 and gone.json()["status"] == "deleted", gone.text
    assert task_id not in {t["id"] for t in client.get(f"{B}/report-tasks", headers=admin).json()}   # 修前照列
    assert task_id in {t["id"] for t in client.get(f"{B}/report-tasks", headers=admin,
                                                   params={"status": "deleted"}).json()}   # 明说要看的照给
    back = client.patch(f"{B}/report-tasks/{task_id}", headers=admin, json={"status": "active"})
    assert back.status_code == 409, back.text   # 修前 200：删掉的又天天推送
    run = client.post(f"{B}/report-instances", headers=admin, json={"task_id": task_id})
    assert run.status_code == 409, run.text   # 修前 201


def test_没删的照旧(client, admin):
    task_id = _task(client, admin, "P2832 照旧")
    assert task_id in {t["id"] for t in client.get(f"{B}/report-tasks", headers=admin).json()}
    paused = client.patch(f"{B}/report-tasks/{task_id}", headers=admin, json={"status": "paused"})
    assert paused.status_code == 200 and paused.json()["status"] == "paused", paused.text
    assert task_id in {t["id"] for t in client.get(f"{B}/report-tasks", headers=admin).json()}   # 暂停的照列
