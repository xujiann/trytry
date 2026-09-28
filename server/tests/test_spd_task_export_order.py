"""任务中心「导出 CSV」截断时取的是表格上的前 N 条（第十六批「导出 vs 页面」扫描 T1-2）。

清单按「优先级高 → 截止早 → 新建」排，导出却按编号倒序截取：超过上限时留下的是最新建的普通任务，表格排在最前的
特急、快到期的反被截掉。中心调度手册说导出「与表格同一口径」、截断时回执写明「只导出了前 N 条」——那个「前」得是
表格上的前。修后两处共用一个排序。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "T1-2 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "T1-2 患者", "id_card": "330281197705052217"}).json()["id"]
    titles = [(f"T12 特急-{i}", 3) for i in range(3)] + [(f"T12 普通-{i}", 1) for i in range(5)]
    for title, priority in titles:   # 先建特急（编号小），后建普通
        resp = client.post(f"{B}/tasks", headers=admin, json={
            "patient_id": patient, "title": title, "org_id": org, "priority": priority, "due_days": 7})
        assert resp.status_code == 201, resp.text
    return {"org": org}


def test_导出截断取的与清单前N条是同一批(client, admin, world):
    listed = [t["title"] for t in client.get(f"{B}/tasks?org_id={world['org']}&limit=3", headers=admin).json()]
    exported = client.get(f"{B}/tasks-export?org_id={world['org']}&limit=3", headers=admin).json()
    assert (exported["matched"], exported["total"]) == (8, 3)
    title_col = exported["columns"].index("标题")
    assert [r[title_col] for r in exported["rows"]] == listed == ["T12 特急-2", "T12 特急-1", "T12 特急-0"]  # 修前导出的是普通-4/3/2
