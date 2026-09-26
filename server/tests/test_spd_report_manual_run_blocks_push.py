"""报告「立即执行」挡掉当期的全域定时报告：谁在本机构点一下，全县那份就不出了、订阅人也收不到（P2-255）。

定时推送（`jobs.spd_report_push`）按「任务 + 期间标签」判「这一期出过没有」：没绑机构的任务出一份全域的，标签就是
期间本身；绑了机构的每家一份，标签带「·机构N」。「立即执行」（`POST /api/spd/report-instances` 带任务号）按本人
机构出一份，标签却不带机构后缀——与全域那份的标签一模一样，定时推送判成「已出过」，当期的全域报告不再生成。

修法：判重连机构一起判（全域的只认机构为空的实例）。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2255 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    tpl = client.post(f"{B}/report-templates", headers=admin, json={
        "code": "P2255_TPL", "name": "P2255 日报", "period": "daily", "sections": [{"type": "kpi", "title": "在管"}]})
    assert tpl.status_code == 201, tpl.text
    director = client.post("/api/users", headers=admin, json={
        "username": "p2255_dir", "password": "Passw0rd!x", "role": "director", "org_id": org})
    assert director.status_code == 201, director.text
    token = client.post("/api/auth/login", json={"username": "p2255_dir", "password": "Passw0rd!x"}).json()
    task = client.post(f"{B}/report-tasks", headers=admin, json={
        "template_id": tpl.json()["id"], "name": "P2255 全县日报", "push_time": "00:00",
        "subscriber_ids": [director.json()["id"]]})
    assert task.status_code == 201, task.text
    return {"org": org, "task": task.json()["id"], "director": director.json()["id"],
            "auth": {"Authorization": f"Bearer {token['access_token']}"}}


def _instances(task_id):
    from app.database import SessionLocal
    from app.spd.models import SpdReportInstance

    with SessionLocal() as db:
        return [(i.org_id, i.period_label) for i in
                db.query(SpdReportInstance).filter_by(task_id=task_id).order_by(SpdReportInstance.id)]


def test_本机构立即执行之后_定时推送照出当期全域报告(client, world):
    from app.database import SessionLocal
    from app.spd.jobs import spd_report_push

    manual = client.post(f"{B}/report-instances", headers=world["auth"], json={"task_id": world["task"]})
    assert manual.status_code == 201, manual.text
    period = manual.json()["period_label"]
    assert _instances(world["task"]) == [(world["org"], period)]
    with SessionLocal() as db:
        spd_report_push(db)
        db.commit()   # 调度框架在任务返回后提交，这里直接调函数、自己提交
    # 修前只有手动那一份：定时推送把它当成当期的全域报告，跳过不出
    assert _instances(world["task"]) == [(world["org"], period), (None, period)]
    with SessionLocal() as db:   # 同一期再跑一轮不重复出
        spd_report_push(db)
        db.commit()
    assert len(_instances(world["task"])) == 2
