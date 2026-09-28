"""任务中心「导出 CSV」与清单同一个筛选口径、截断看得见（P2-526）。

- 「只看我的」：清单按承办人是自己筛，导出端点却不收这个参数，页面发请求前还把它删掉（注释写着「导出端点没有
  mine 参数：它按调用方可见机构导出」）——勾着「只看我的」点导出，拿到的是本机构所有人的任务，与屏幕上那张表对不上。
- 截断：导出截在 2000 行，回执只说「已导出 N 条（上限 2000……）」，恰好 2000 条时分不清是全部还是被截了。

修后导出收 `mine`、与清单同一个判据；出参加 `matched`（同一筛选下命中的总数），页面在 `matched > total` 时明说
「共 M 条，只导出了前 N 条」。
"""
from pathlib import Path

import pytest

from conftest import login

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2526 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    doctors = {}
    for key in ("a", "b"):
        created = client.post("/api/users", headers=admin, json={
            "username": f"p2526_doc_{key}", "password": "passw0rd1", "role": "doctor",
            "full_name": f"P2526 医生{key}", "org_id": org})
        assert created.status_code == 201, created.text
        doctors[key] = created.json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2526 患者", "id_card": "330455199505052526"}).json()["id"]
    tasks = {}
    for key, n in (("a", 2), ("b", 3)):
        tasks[key] = []
        for i in range(n):
            resp = client.post("/api/spd/tasks", headers=admin, json={
                "patient_id": patient, "title": f"P2526 医生{key}的任务{i}", "task_type": "followup",
                "org_id": org, "assignee_id": doctors[key], "due_days": 7})
            assert resp.status_code == 201, resp.text
            tasks[key].append(resp.json()["id"])
    return {"org": org, "doctors": doctors, "tasks": tasks, "patient": patient,
            "h_a": login(client, "p2526_doc_a", "passw0rd1")}


def _ids(body) -> set[int]:
    return {row[0] for row in body["rows"]}


def test_只看我的_导出与清单同一个判据(client, world):
    listed = client.get("/api/spd/tasks", headers=world["h_a"], params={"mine": "true", "limit": 500})
    assert listed.status_code == 200, listed.text
    exported = client.get("/api/spd/tasks-export", headers=world["h_a"], params={"mine": "true"})
    assert exported.status_code == 200, exported.text
    assert _ids(exported.json()) == {t["id"] for t in listed.json()} == set(world["tasks"]["a"])
    # 修前：导出不认 mine，医生乙的 3 条也在里面


def test_不勾只看我的_照旧导出可见机构的全部(client, world):
    exported = client.get("/api/spd/tasks-export", headers=world["h_a"]).json()
    assert set(world["tasks"]["a"]) | set(world["tasks"]["b"]) <= _ids(exported)


def test_截断看得见_matched是同一筛选下命中的总数(client, world):
    body = client.get("/api/spd/tasks-export", headers=world["h_a"],
                      params={"org_id": world["org"], "limit": 2}).json()
    assert body["total"] == len(body["rows"]) == 2
    assert body["matched"] == 5   # 修前没有这个键：恰好截在上限时分不清是全部还是被截了


def test_页面不再删掉只看我的_并按matched明说截断():
    src = (STATIC / "pages-spd.js").read_text(encoding="utf-8")
    start = src.index("if (exportBtn) {")
    handler = src[start:src.index("return;", start)]
    assert "delete filters.mine" not in handler
    assert "d.matched > d.total" in handler


def test_按团队筛_导出与清单同一个判据(client, admin, world):
    """P2-685：任务中心筛选栏补了机构 / 团队（中心调度手册写「按机构 / 团队 / 类型筛出超期任务」，原先只有类型、状态、
    只看我的）。清单本就收 team_id，导出原先不收——按团队筛了再导出，拿到的是全机构的。"""
    team = client.post("/api/spd/teams", headers=admin, json={"name": "P2685 甲组", "org_id": world["org"]})
    assert team.status_code == 201, team.text
    in_team = []
    for i in range(2):
        created = client.post("/api/spd/tasks", headers=admin, json={
            "patient_id": world["patient"], "title": f"P2685 团队任务{i}", "task_type": "followup",
            "org_id": world["org"], "team_id": team.json()["id"], "due_days": 7})
        assert created.status_code == 201, created.text
        in_team.append(created.json()["id"])
    listed = client.get("/api/spd/tasks", headers=admin, params={"team_id": team.json()["id"], "limit": 500}).json()
    exported = client.get("/api/spd/tasks-export", headers=admin, params={"team_id": team.json()["id"]}).json()
    assert {t["id"] for t in listed} == _ids(exported) == set(in_team)   # 修前导出不认 team_id，全机构 7 条都在


def test_筛选栏有机构与团队两项():
    src = (STATIC / "pages-spd.js").read_text(encoding="utf-8")
    form = src[src.index('<form class="inline" id="spd-task-filter">'):src.index("</form>", src.index('id="spd-task-filter"'))]
    assert 'name="org_id"' in form and 'name="team_id"' in form   # 修前只有类型、状态、只看我的
