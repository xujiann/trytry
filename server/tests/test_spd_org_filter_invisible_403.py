"""慢专病清单、导出、汇总、工作量点名看不见的机构时 403，不再悄悄回空表（P2-831，第二十二批「页面查询参数 vs 后端」
扫描 X4-9）。

这几处先按可见范围过滤、再按 `org_id` 等值——乡镇医生按所辖村卫生室筛任务，得到 200 空表、X-Total-Count 0，导出
matched 0，工作量 []，看的人以为那家机构没有数据。平台同一个筛选是 403：`visibility.scope_org_list` 写明「不是悄悄返回
空——悄悄返回空会让人以为那家机构没数据」。修后带 `org_id` 时先 `assert_org_visible`。工作台的机构筛选是统计范围
（`workbench._scope`），不在本条。
"""
import pytest

from conftest import login

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    town = client.post("/api/organizations", headers=admin, json={
        "name": "P2831 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    village = client.post("/api/organizations", headers=admin, json={
        "name": "P2831 村卫生室", "org_type": "village", "level": "village", "parent_id": town}).json()["id"]
    resp = client.post("/api/users", headers=admin, json={
        "username": "p2831_doc", "password": "passw0rd1", "full_name": "p2831_doc", "role": "doctor", "org_id": town})
    assert resp.status_code in (200, 201), resp.text
    return {"town": town, "village": village, "head": login(client, "p2831_doc", "passw0rd1")}


@pytest.mark.parametrize("path, params", [
    ("/tasks", {}), ("/tasks-export", {}), ("/tasks/summary", {}), ("/workload", {}),
    ("/candidates", {}), ("/enrollments", {}),
])
def test_点名看不见的机构403_本机构照旧(client, world, path, params):
    other = client.get(f"{B}{path}", headers=world["head"], params={**params, "org_id": world["village"]})
    assert other.status_code == 403 and other.json()["detail"] == "无权查看该机构的数据", other.text   # 修前 200 空
    own = client.get(f"{B}{path}", headers=world["head"], params={**params, "org_id": world["town"]})
    assert own.status_code == 200, own.text


def test_按患者查任务照旧不按机构收(client, admin, world):
    """按患者查的一支按患者可见性判（跨机构的任务都列），带上别家的 org_id 只是在里面再筛，不 403。"""
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2831 患者", "id_card": "330106197007072831"}).json()["id"]
    got = client.get(f"{B}/tasks", headers=admin, params={"patient_id": patient, "org_id": world["village"]})
    assert got.status_code == 200 and got.json() == [], got.text
