"""已结项 / 已中止的项目，里程碑照样能「完成 / 撤销完成」（P2-1223，第三十五批「封存之后的改动」扫描 T2-5）。

加里程碑对这两态早就 409「项目已完成或已中止，不能再加里程碑」（P1-104），页面也不给「加里程碑」（P2-597）；里程碑的完成 /
撤销完成却不看项目状态：结了项（已完成、进度 100%）的项目撤销「上线验收」的完成照 200，项目成了「已完成 100%、里程碑完成
1 / 2」；已中止的项目照样能把里程碑标完成、撤销完成。

修法：完成 / 撤销完成对已完成 / 已中止的项目 409，与加里程碑同一句式；里程碑面板上这两种项目的里程碑不给「完成」「撤销
完成」。要改先用「报进度」把项目状态改回「进行中」（与 P2-597 同一条路），改回来照旧能撤销——误点了照样放得开。
"""
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")
CLOSED = "项目已完成或已中止，不能再改里程碑"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21223 卫生院", "org_type": "township", "level": "township"}).json()["id"]

    def project(name, status):
        created = client.post("/api/projects", headers=admin, json={"org_id": org, "name": name, "due_date": "2026-12-31"})
        assert created.status_code == 201, created.text
        pid = created.json()["id"]
        done, todo = (client.post(f"/api/projects/{pid}/milestones", headers=admin, json={
            "name": n, "due_date": "2026-06-01"}).json()["id"] for n in ("需求调研", "上线验收"))
        assert client.post(f"/api/projects/milestones/{done}/done?done_date=2026-03-01", headers=admin).status_code == 200
        assert client.patch(f"/api/projects/{pid}", headers=admin, json=status).status_code == 200
        return {"id": pid, "done": done, "todo": todo}

    return {
        "结项": project("P21223 已结项", {"status": "done", "progress_pct": 100}),
        "中止": project("P21223 已中止", {"status": "suspended"}),
        "在办": project("P21223 进行中", {"status": "ongoing", "progress_pct": 40}),
    }


def _project(client, admin, pid):
    return client.get(f"/api/projects/{pid}", headers=admin).json()


def _snapshot(row):
    return (row["status"], row["progress_pct"], row["milestone_done"], row["milestone_total"],
            [(m["id"], m["done"], m["done_date"]) for m in row["milestones"]])


@pytest.mark.parametrize("tag", ["结项", "中止"])
def test_结项或中止的项目_里程碑不能再完成或撤销完成(client, admin, world, tag):
    target = world[tag]
    before = _snapshot(_project(client, admin, target["id"]))
    reopened = client.post(f"/api/projects/milestones/{target['done']}/reopen", headers=admin)
    assert reopened.status_code == 409, reopened.text   # 修前 200：结了项的成了「已完成 100%、里程碑完成 1 / 2」
    assert reopened.json()["detail"] == CLOSED   # 与加里程碑同一句式
    completed = client.post(f"/api/projects/milestones/{target['todo']}/done", headers=admin)
    assert completed.status_code == 409, completed.text   # 修前 200
    assert completed.json()["detail"] == CLOSED
    assert _snapshot(_project(client, admin, target["id"])) == before   # 里程碑与进度原样


def test_进行中的项目照旧能完成与撤销完成(client, admin, world):
    target = world["在办"]
    completed = client.post(f"/api/projects/milestones/{target['todo']}/done", headers=admin)
    assert completed.status_code == 200 and completed.json()["done"] is True, completed.text
    reopened = client.post(f"/api/projects/milestones/{target['done']}/reopen", headers=admin)
    assert reopened.status_code == 200 and reopened.json()["done"] is False, reopened.text


def test_结项后改回进行中_照旧能撤销完成(client, admin, world):
    """误点了照样放得开：先用「报进度」把状态改回进行中（与加里程碑 P2-597 同一条路）。"""
    target = world["结项"]
    assert client.patch(f"/api/projects/{target['id']}", headers=admin, json={"status": "ongoing"}).status_code == 200
    reopened = client.post(f"/api/projects/milestones/{target['done']}/reopen", headers=admin)
    assert reopened.status_code == 200 and reopened.json()["done"] is False, reopened.text


def test_里程碑面板只给在办项目的里程碑画完成与撤销完成():
    start = PAGE.index('${panel("里程碑（全部项目）"')
    body = PAGE[start:PAGE.index("$(\"#pj-form\")", start)]
    assert ('projects.flatMap((p) => p.milestones.map((m) => '
            '({ project: p.name, closed: p.status === "done" || p.status === "suspended", m })))') in body
    ops = body[body.index("<td>${closed"):]
    assert ops.index('<td>${closed ? "—"') < ops.index('data-msreopen="${m.id}"') < ops.index('data-msdone="${m.id}"')   # 修前无条件画
    assert body.count('data-msreopen="${m.id}"') == 1 and body.count('data-msdone="${m.id}"') == 1
