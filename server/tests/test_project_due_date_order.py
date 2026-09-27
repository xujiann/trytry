"""改项目的计划完成日可以改到开始日之前（P2-415）。

建项目时「计划完成日期不得早于开始日期」（422），`PATCH /api/projects/{id}` 改计划完成日却不查——改完的项目
「开始」晚于「完成」，立项当天就算逾期。修后改的时候同一句 422。
"""
import pytest


@pytest.fixture(scope="module")
def project(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2415 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    created = client.post("/api/projects", headers=admin, json={
        "org_id": org, "name": "P2415 胸痛中心建设", "start_date": "2026-09-01", "due_date": "2026-12-31"})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_改计划完成日到开始日之前_422(client, admin, project):
    got = client.patch(f"/api/projects/{project}", headers=admin, json={"due_date": "2026-08-01"})
    assert (got.status_code, got.json()["detail"]) == (422, "计划完成日期不得早于开始日期"), got.text   # 修前 200
    assert client.get(f"/api/projects/{project}", headers=admin).json()["due_date"] == "2026-12-31"


def test_改到开始日之后照常(client, admin, project):
    got = client.patch(f"/api/projects/{project}", headers=admin, json={"due_date": "2026-09-01"})
    assert got.status_code == 200 and got.json()["due_date"] == "2026-09-01", got.text
