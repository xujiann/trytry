"""随访中心「超期未随访」行上的「完成随访」按「这一行当前用户能不能办」摆（P2-1313，第三十八批扫描 AB1-4 的随访那一半）。

超期清单（`GET /api/followups/overdue`）不认调用方、按全县列最多 500 条，页面标题是 `overdue.length`、每行都摆「完成随访」；
完成接口却以任务机构的名义写（`assert_obj_org_writable`）。修前实测（scan38 ab1/r4）：东镇医生看到「超期未随访（4）」，其中
3 条是西镇的，点 #1 完成 200、点 #2 完成 403「无权以该机构名义写入数据」；同页「待随访任务」本来就按机构收口，只有东镇那条。

修法照 P2-793（`tests/test_row_actionable_flags.py`）：超期清单补调用方身份，行上加 `can_handle`（`visibility.can_write_org`，与完成 /
取消的 `assert_obj_org_writable` 同一判据，两者逐组合一致由 P2-793 那条用例钉着），页面只给能办的行摆按钮、标题写明「其中本机构
可办 X」。清单给谁看不改（随 P1-49 / P2-609 待裁定），按角色摆不摆随 P2-447。
"""
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import business_today, login

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {}
    for key, name in (("east", "P21313 东镇卫生院"), ("west", "P21313 西镇卫生院")):
        resp = client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township", "level": "township"})
        assert resp.status_code == 201, resp.text
        orgs[key] = resp.json()["id"]
    heads = {}
    for key in orgs:
        username = f"p21313_fu_{key}"
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "full_name": username, "role": "doctor", "org_id": orgs[key]})
        assert resp.status_code in (200, 201), resp.text
        heads[key] = login(client, username, "passw0rd1")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21313 随访患者", "id_card": "330106197001011313"}).json()["id"]
    past = (business_today() - timedelta(days=10)).isoformat()
    tasks = {}
    for key, n in (("east", 1), ("west", 3)):
        tasks[key] = []
        for _ in range(n):
            resp = client.post("/api/followups", headers=heads[key], json={
                "patient_id": patient, "org_id": orgs[key], "category": "chronic", "due_date": past})
            assert resp.status_code == 201, resp.text
            tasks[key].append(resp.json()["id"])
    return {"heads": heads, "tasks": tasks}


def _flags(client, headers) -> dict[int, bool]:
    resp = client.get("/api/followups/overdue", headers=headers)
    assert resp.status_code == 200, resp.text
    return {r["id"]: r["can_handle"] for r in resp.json()}   # 修前没有这个键


def test_超期清单_本机构的行能办_别家的不能办(client, admin, world):
    east, west = world["tasks"]["east"], world["tasks"]["west"]
    for who, mine, theirs in (("east", east, west), ("west", west, east)):
        flags = _flags(client, world["heads"][who])
        assert set(flags) >= set(east + west), who   # 清单给谁看不改：照旧全县
        assert [flags[t] for t in mine] == [True] * len(mine), who
        assert [flags[t] for t in theirs] == [False] * len(theirs), who
    admin_flags = _flags(client, admin)
    assert all(admin_flags[t] for t in east + west)   # 全域角色放行


def test_标记与完成接口同一口径(client, world):
    """标 false 的点了 403（修前页面照样摆按钮），标 true 的点了 200。"""
    heads, east, west = world["heads"], world["tasks"]["east"], world["tasks"]["west"]
    for task in west:
        denied = client.post(f"/api/followups/{task}/complete", headers=heads["east"], json={"result": "电话随访，血压平稳"})
        assert denied.status_code == 403, denied.text
    done = client.post(f"/api/followups/{east[0]}/complete", headers=heads["east"], json={"result": "电话随访，血压平稳"})
    assert done.status_code == 200, done.text
    assert east[0] not in _flags(client, heads["east"])   # 办完就不再超期
    assert all(_flags(client, heads["west"])[t] for t in west)


def test_页面只给能办的行摆按钮_标题写明本机构可办几条():
    source = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    start = source.index("async function renderFollowups()")
    body = source[start:source.index("\nasync function ", start + 1)]
    assert "const handleable = overdue.filter((t) => t.can_handle).length;" in body
    assert "panel(`超期未随访（${overdue.length}，其中本机构可办 ${handleable}）`" in body   # 修前只有 overdue.length
    overdue_panel = body[body.index("panel(`超期未随访"):body.index("panel(`待随访任务")]
    assert ('${t.can_handle ? `<button class="btn secondary" data-done="${t.id}">完成随访</button>` : "—"}'
            in overdue_panel)   # 修前每行都摆
