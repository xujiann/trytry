"""生命周期清单只给迁入机构摆「确认迁入」：清单行带 `can_confirm`，与确认接口同一判据现算（P2-829，第二十二批「页面
查询参数 vs 后端」扫描 X4-5；清单本身给谁看仍随 P1-49 待裁定）。

`confirm_migration` 判 `assert_org_writable(target_org_id)`——确认由迁入机构做；工作台「待确认迁入」也只数迁到本范围的
（P2-61）。页面取 `lifecycle-events?event=migrate&confirmed=false` 排在最前，只要不是作废的就画按钮：迁出方与无关的第三家
机构，清单最前面都是别家的「待确认」，点下去 403「无权以该机构名义写入数据」。修后按 `can_confirm` 摆。
"""
from pathlib import Path

import pytest

from conftest import login

B = "/api/spd"
PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {}
    for key in ("甲", "乙", "丙"):
        orgs[key] = client.post("/api/organizations", headers=admin, json={
            "name": f"P2829 {key}卫生院", "org_type": "township", "level": "township"}).json()["id"]
    heads = {}
    for key in orgs:
        resp = client.post("/api/users", headers=admin, json={
            "username": f"p2829_{key}", "password": "passw0rd1", "full_name": f"p2829_{key}", "role": "doctor",
            "org_id": orgs[key]})
        assert resp.status_code in (200, 201), resp.text
        heads[key] = login(client, f"p2829_{key}", "passw0rd1")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2829 迁出患者", "id_card": "330106196505052829"}).json()["id"]
    enrolled = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": orgs["甲"]})
    assert enrolled.status_code == 201, enrolled.text
    moved = client.post(f"{B}/enrollments/{enrolled.json()['id']}/lifecycle", headers=heads["甲"], json={
        "event": "migrate", "reason": "搬家", "target_org_id": orgs["乙"]})
    assert moved.status_code == 200 and moved.json()["pending_confirm"] is True, moved.text
    return {"orgs": orgs, "heads": heads, "event": moved.json()["event_id"]}


def _can_confirm(client, headers, event_id):
    rows = client.get(f"{B}/lifecycle-events", headers=headers,
                      params={"event": "migrate", "confirmed": "false", "limit": 200}).json()
    return {r["id"]: r["can_confirm"] for r in rows}.get(event_id)


def test_只有迁入机构能确认_与接口一致(client, world):
    heads, event = world["heads"], world["event"]
    assert _can_confirm(client, heads["甲"], event) is False   # 迁出方：修前没有这个键，页面照画按钮
    assert _can_confirm(client, heads["丙"], event) is False   # 无关的第三家
    for key in ("甲", "丙"):
        denied = client.post(f"{B}/lifecycle-events/{event}/confirm", headers=heads[key])
        assert denied.status_code == 403, denied.text
    assert _can_confirm(client, heads["乙"], event) is True
    ok = client.post(f"{B}/lifecycle-events/{event}/confirm", headers=heads["乙"])
    assert ok.status_code == 200, ok.text
    assert _can_confirm(client, heads["乙"], event) is None   # 确认过的不再是待确认


def test_页面按标记摆确认迁入():
    assert 'v.can_confirm ? `<button class="btn secondary" data-confirm="${v.id}">确认迁入</button>` : "待迁入机构确认"' in PAGE
    assert "const rows = actionableFirst(recent, pending.filter((v) => v.can_confirm));" in PAGE
