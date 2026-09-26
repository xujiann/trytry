"""报告推送任务「启用中」却跑不起来的两种写法照收：有效期止早于今天、模板停用后改回启用（P2-297）。

建任务时停用的模板、倒置的有效期都已挡住——理由写在代码里：「照样 201、之后天天被跳过」，任务显示「启用中」，
调度（`jobs.spd_report_push`）一份报告也不出。同一种形状还剩两个口子：
- 有效期止早于今天：调度按 `今天 > 止` 跳过，建出来就是死任务；启用中的任务把止期改到过去同理；
- 模板停用后，把暂停的任务改回启用：改档不查模板，照样 200。

修法：以「启用」落库的，止期不得早于今天（止于今天当天照跑）；改回启用时查模板在且启用。只在动了状态或止期时查——
启用中的任务改推送时点、改订阅人，不被后来停掉的模板拦住；暂停 / 删除更不拦。
"""
from datetime import date

import pytest
from conftest import freeze_business_date

B = "/api/spd"
TODAY = date(2026, 9, 26)


@pytest.fixture(scope="module")
def template(client, admin):
    created = client.post(f"{B}/report-templates", headers=admin, json={
        "code": "P2297_T", "name": "P2297 周报", "period": "weekly", "sections": [{"key": "summary", "title": "概况"}]})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _task(client, admin, template, **extra):
    return client.post(f"{B}/report-tasks", headers=admin,
                       json={"template_id": template, "name": "P2297 推送", **extra})


def test_建任务_有效期止已过_422_止于今天照收(client, admin, template):
    with freeze_business_date(TODAY):
        expired = _task(client, admin, template, valid_to="2026-09-25")
        assert expired.status_code == 422, expired.text   # 修前 201
        assert "已过" in expired.json()["detail"]
        assert _task(client, admin, template, valid_to="2026-09-26").status_code == 201


def test_启用中把止期改到过去_422_暂停的照改(client, admin, template):
    with freeze_business_date(TODAY):
        task = _task(client, admin, template).json()["id"]
        got = client.patch(f"{B}/report-tasks/{task}", headers=admin, json={"valid_to": "2026-09-01"})
        assert got.status_code == 422, got.text   # 修前 200
        paused = client.patch(f"{B}/report-tasks/{task}", headers=admin, json={"status": "paused", "valid_to": "2026-09-01"})
        assert paused.status_code == 200, paused.text
        # 过了期的暂停任务改回启用，同样挡住
        back = client.patch(f"{B}/report-tasks/{task}", headers=admin, json={"status": "active"})
        assert back.status_code == 422, back.text   # 修前 200


def test_模板停用后_暂停的任务改回启用404_启用中的改推送时点不拦(client, admin):
    created = client.post(f"{B}/report-templates", headers=admin, json={
        "code": "P2297_T2", "name": "P2297 月报", "period": "monthly", "sections": [{"key": "summary"}]})
    assert created.status_code == 201, created.text
    template = created.json()["id"]
    paused = _task(client, admin, template).json()["id"]
    running = _task(client, admin, template).json()["id"]
    assert client.patch(f"{B}/report-tasks/{paused}", headers=admin, json={"status": "paused"}).status_code == 200
    assert client.patch(f"{B}/report-templates/{template}", headers=admin, json={"active": False}).status_code == 200

    back = client.patch(f"{B}/report-tasks/{paused}", headers=admin, json={"status": "active"})
    assert back.status_code == 404 and back.json()["detail"] == "报告模板不存在或已停用", back.text   # 修前 200
    moved = client.patch(f"{B}/report-tasks/{running}", headers=admin, json={"push_time": "09:30"})
    assert moved.status_code == 200 and moved.json()["push_time"] == "09:30", moved.text
