"""报告推送绑了几家机构，每份报告与「报告已生成」的标题都带机构名（P2-891，第二十四批「通知、提醒与待办」扫描 Z2-9）。

推送任务绑 3 家机构、1 个订阅人，跑一轮出 3 份报告、订阅人收 3 条「报告已生成：慢病管理月报（2026年09月）」——
标题与正文一字不差，看不出是哪家的；报告清单上 3 行标题相同，只在期间列里有「·机构3」这种编号。修后绑了机构的那几份
标题带机构名（没绑机构的全域报告照旧）；判重用的期间标签不动。
"""
from app.database import SessionLocal
from app.models import Notification
from app.spd.models import SpdReportInstance

B = "/api/spd"


def test_绑三家机构_三条消息标题各带机构名(client, admin):
    orgs = {client.post("/api/organizations", headers=admin, json={
        "name": f"P2891 片区卫生院{i}", "org_type": "township", "level": "township"}).json()["id"]: f"P2891 片区卫生院{i}"
        for i in range(1, 4)}
    sub = client.post("/api/users", headers=admin, json={
        "username": "p2891_dir", "password": "Passw0rd!x", "role": "director", "org_id": None})
    assert sub.status_code == 201, sub.text
    tpl = client.post(f"{B}/report-templates", headers=admin, json={
        "code": "P2891_TPL", "name": "P2891 月报", "sections": [{"type": "kpi", "title": "在管"}]})
    assert tpl.status_code == 201, tpl.text
    task = client.post(f"{B}/report-tasks", headers=admin, json={
        "template_id": tpl.json()["id"], "name": "P2891 推送", "push_time": "00:00",
        "subscriber_ids": [sub.json()["id"]], "org_ids": list(orgs)})
    assert task.status_code == 201, task.text
    run = client.post("/api/jobs/spd_report_push/run", headers=admin)
    assert run.status_code == 201 and run.json()["status"] == "succeeded", run.text[:300]
    with SessionLocal() as db:
        instances = db.query(SpdReportInstance).filter(SpdReportInstance.task_id == task.json()["id"]).all()
        titles = {i.org_id: i.title for i in instances}
        notes = sorted(title for (title,) in db.query(Notification.title).filter(
            Notification.user_id == sub.json()["id"], Notification.link_type == "spd_report_instance"))
    assert set(titles) == set(orgs)
    for org_id, name in orgs.items():
        assert titles[org_id].startswith("P2891 月报（") and titles[org_id].endswith(f"·{name}）"), titles
    assert len(set(notes)) == 3 and all(n.startswith("报告已生成：P2891 月报（") for n in notes), notes   # 修前三条一字不差
