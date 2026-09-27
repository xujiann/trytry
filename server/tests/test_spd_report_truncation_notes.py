"""慢专病报告的待办 / 超期 / 积分表截断了要写明（P2-645，第十四批「导出 / 打印 vs 页面」扫描 R1-4）。

「总体概览」写着「待办任务 45 条，其中超期 23 条」，下面「待办任务」「超期预警」两张表各只列 20 行、一个字不提，读的人
以为全在这了；「村医积分」列 10 个账户也不说一共多少。同一份报告里考核排名段已经写「共 N 个，列前 20 名」（P2-638）。
待办表按截止日排序原先没有尾键：同一天截止的多于能列的，两次生成列的是不同的几条。

修法：三张表截断时带 `note`（「共 N 条待办，列截止最早的 20 条」之类），没截断的不写；待办表补 id 尾键。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdPointAccount, SpdTask

    def org(name):
        return client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township", "level": "township"}).json()["id"]

    big, small = org("P2645 大卫生院"), org("P2645 小卫生院")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2645 患者", "id_card": "330106197505052645"}).json()["id"]
    users = [client.post("/api/users", headers=admin, json={
        "username": f"p2645_u{i}", "password": "Passw0rd!2645", "role": "doctor", "full_name": f"P2645 医生{i}",
        "org_id": big}).json()["id"] for i in range(12)]
    with SessionLocal() as db:
        todo = [SpdTask(patient_id=patient, org_id=big, program_code="P2645_PG", task_type="followup",
                        title=f"P2645 待办{i:02d}", status="pending", due_date="2099-06-01") for i in range(25)]
        overdue = [SpdTask(patient_id=patient, org_id=big, program_code="P2645_PG", task_type="followup",
                           title=f"P2645 超期{i:02d}", status="overdue", due_date="2020-06-01") for i in range(23)]
        db.add_all(todo + overdue)
        db.add(SpdTask(patient_id=patient, org_id=small, program_code="P2645_PG", task_type="followup",
                       title="P2645 小院待办", status="pending", due_date="2099-06-01"))
        for i, user_id in enumerate(users):
            db.add(SpdPointAccount(user_id=user_id, org_id=big, balance=100 - i, earned=100 - i, used=0))
        db.commit()
        return {"big": big, "small": small, "todo": [t.id for t in todo]}


def _section(key, org):
    from app.database import SessionLocal
    from app.spd.reporting import compose_section

    with SessionLocal() as db:
        return compose_section(db, {"key": key}, org, "daily")


def test_概览的数与两张表写明的总数对得上(world):
    summary = _section("summary", world["big"])["metrics"]
    todo, alert = _section("todo", world["big"]), _section("alert", world["big"])
    assert (summary["open_tasks"], summary["overdue"]) == (48, 23)
    assert todo["note"] == "共 25 条待办，列截止最早的 20 条"   # 修前没有 note
    assert alert["note"] == "共 23 条超期，列截止最早的 20 条"
    assert len(todo["rows"]) == len(alert["rows"]) == 20


def test_同一天截止的按先后收尾(world):
    rows = _section("todo", world["big"])["rows"]
    assert [r[0] for r in rows] == [f"P2645 待办{i:02d}" for i in range(20)]


def test_积分表截断写明(world):
    out = _section("points", world["big"])
    assert out["note"] == "共 12 个积分账户，列余额前 10 名" and len(out["rows"]) == 10


def test_没截断的不写(world):
    for key in ("todo", "alert", "points"):
        assert "note" not in _section(key, world["small"]), key
