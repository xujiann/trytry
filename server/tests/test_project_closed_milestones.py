"""已完成 / 已中止的项目，挂着的里程碑不再算逾期（P2-175）。

项目本身的逾期判断早把「已完成 / 已中止」排除在外；P1-104 又拦了给这两态的项目新加里程碑，理由写得明白：
「新加的里程碑却照样按到期日算逾期，一个结了项的项目挂着『逾期里程碑 1』」。可结项前就挂着、没勾完成的里程碑
照样按到期日算逾期——中止的项目、结了项的项目照旧挂着「逾期里程碑 N」，页面上标黄。
"""
import pytest


@pytest.fixture(scope="module")
def project(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2175 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    created = client.post("/api/projects", headers=admin, json={"org_id": org, "name": "P2175 信息化改造"})
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    ms = client.post(f"/api/projects/{pid}/milestones", headers=admin, json={"name": "机房验收", "due_date": "2026-01-15"})
    assert ms.status_code == 201 and ms.json()["overdue"] is True, ms.text   # 在办时照常算逾期
    return {"id": pid, "milestone": ms.json()["id"]}


def _row(client, admin, pid):
    return client.get(f"/api/projects/{pid}", headers=admin).json()


def test_中止的项目里程碑不算逾期(client, admin, project):
    resp = client.patch(f"/api/projects/{project['id']}", headers=admin, json={"status": "suspended"})
    assert resp.status_code == 200, resp.text
    row = _row(client, admin, project["id"])
    assert row["milestone_overdue"] == 0   # 修前 1
    assert [m["overdue"] for m in row["milestones"]] == [False]


def test_中止后撤销完成的里程碑同样不算逾期(client, admin, project):
    assert client.post(f"/api/projects/milestones/{project['milestone']}/done", headers=admin).status_code == 200
    reopened = client.post(f"/api/projects/milestones/{project['milestone']}/reopen", headers=admin)
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["overdue"] is False   # 修前 True
