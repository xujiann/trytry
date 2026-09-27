"""超期任务数不靠「刚扫过一次」：扫描间隙里过了截止日的在手任务同样算超期（P2-549，第十批「同源数字承诺」扫描 X2-4）。

超期靠 `sweep_overdue` 落状态；中心工作台与任务清单进门先扫，卫健 / 团队 / 医生移动端工作台、报告与考核取数只数
`status == 'overdue'`——调度没跑或两次扫描之间，同一批过期任务在这些地方是 0，在中心工作台是 N。随访早有
`followup_overdue`（已标超期 + 间隙里过期的）给各处共用，任务没有。修后任务也有同形状的 `task_overdue`。
"""
import pytest

PROGRAM = "P2549_PG"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2549 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2549 患者", "id_card": "330106197205052549", "gender": "男", "birth_date": "1972-05-05"}).json()["id"]
    with SessionLocal() as db:
        for title, status, due in (("P2549 过期未扫", "pending", "2020-01-01"), ("P2549 已标超期", "overdue", "2020-01-02"),
                                   ("P2549 没到期", "claimed", "2099-01-01")):
            db.add(SpdTask(patient_id=patient, org_id=org, program_code=PROGRAM, task_type="followup", title=title,
                           status=status, due_date=due))
        db.commit()
    return {"org": org}


def test_卫健工作台的超期数含扫描间隙里过期的(client, admin, world):
    resp = client.get("/api/spd/workbench/health-commission", headers=admin,
                      params={"org_id": world["org"], "program_code": PROGRAM})
    assert resp.status_code == 200, resp.text
    assert resp.json()["tasks"]["overdue"] == 2   # 修前 1：只数已标超期的


def test_报告的超期与待办各归其表(world):
    from app.database import SessionLocal
    from app.spd.reporting import compose_section

    with SessionLocal() as db:
        summary = compose_section(db, {"key": "summary", "title": "概况"}, world["org"], "weekly")
        alert = compose_section(db, {"key": "alert", "title": "超期"}, world["org"], "weekly")
        todo = compose_section(db, {"key": "todo", "title": "待办"}, world["org"], "weekly")
    assert summary["metrics"]["overdue"] == 2   # 修前 1
    assert [r[0] for r in alert["rows"]] == ["P2549 过期未扫", "P2549 已标超期"]
    assert [r[0] for r in todo["rows"]] == ["P2549 没到期"]   # 过期未扫的归超期表，不在待办表里重复出现


def test_考核取数的超期与工作台同一个判定(world):
    from app import clock
    from app.database import SessionLocal
    from app.spd.models import SpdIndicator
    from app.spd.routers.assess import collect_metrics

    indicator = SpdIndicator(code="p2549_overdue", name="P2549 超期", data_source="task", object_type="org",
                             formula="overdue")
    with SessionLocal() as db:
        metrics = collect_metrics(db, indicator, "org", world["org"], clock.today().strftime("%Y-%m"))
    assert metrics["overdue"] == 2, metrics   # 修前 1
