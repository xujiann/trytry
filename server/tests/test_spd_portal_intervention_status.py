"""居民端「干预方案」看不出哪条已经不作数了，已移除的还能被标记完成（P2-67）。

医生端干预任务四个状态：待执行 / 执行中 / 已完成 / 已移除（`spd_interventions.status`）。纳管档案结束（死亡、迁出、
排除）时 `close_open_work` 把未办结的干预一律置「已移除」，医生也能手工移除。可居民端 `GET /api/portal/spd/interventions`
只回英文状态码、手机页一个状态都不显示——**已移除、已完成的方案和在执行的方案长得一模一样**，居民照着一份医生已经撤掉
的方案做；`POST …/feedback` 带 `done=true` 不看现状，已移除的一条被居民一点就翻成「已完成」，重新算进完成数。

修法：出参补后端给的中文状态 `status_name`（与医生端同一套：待执行 / 执行中 / 已完成 / 已移除，§13「状态文案取自后端」）；
已移除的不许再标记完成（409），标记已读照常；手机页显示状态、已移除的不再给「标记已读并反馈」按钮。
"""
import pathlib

import pytest
from test_portal_services import login

PHONE = "13700012670"
STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin,
                      json={"name": "P267 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    resp = client.post("/api/spd/programs", headers=admin,
                       json={"code": "p267_prog", "name": "干预状态病种", "category": "chronic"})
    assert resp.status_code == 201, resp.text
    patient = client.post("/api/patients", headers=admin,
                          json={"name": "P267 居民", "id_card": "330192198001011218", "phone": PHONE}).json()
    enroll = client.post("/api/spd/enrollments", headers=admin,
                         json={"patient_id": patient["id"], "program_code": "p267_prog", "org_id": org})
    assert enroll.status_code == 201, enroll.text
    return {"patient": patient, "resident": login(client, PHONE)}


def _intervene(client, admin, world, goal):
    resp = client.post("/api/spd/interventions", headers=admin, json={
        "patient_ids": [world["patient"]["id"]], "program_code": "p267_prog", "goal": goal, "content": "每日步行"})
    assert resp.status_code == 201, resp.text
    items = client.get("/api/portal/spd/interventions", headers=world["resident"]).json()
    return next(i for i in items if i["goal"] == goal)


def _mine(client, world, intervention_id):
    items = client.get("/api/portal/spd/interventions", headers=world["resident"]).json()
    return next(i for i in items if i["id"] == intervention_id)


def test_居民端干预方案带后端给的状态文案(client, admin, world):
    item = _intervene(client, admin, world, "P267 待执行")
    assert item["status"] == "planned" and item["status_name"] == "待执行"   # 修前没有 status_name


def test_已移除的方案不能再被居民标记完成(client, admin, world):
    item = _intervene(client, admin, world, "P267 已移除")
    removed = client.patch(f"/api/spd/interventions/{item['id']}", headers=admin, json={"status": "removed"})
    assert removed.status_code == 200, removed.text
    assert _mine(client, world, item["id"])["status_name"] == "已移除"

    resp = client.post(f"/api/portal/spd/interventions/{item['id']}/feedback",
                       headers=world["resident"], json={"feedback": "照做了", "done": True})
    assert resp.status_code == 409, resp.text   # 修前 200，状态翻成 done
    assert _mine(client, world, item["id"])["status"] == "removed"


def test_已移除的方案照常可以标记已读(client, admin, world):
    item = _intervene(client, admin, world, "P267 移除后已读")
    client.patch(f"/api/spd/interventions/{item['id']}", headers=admin, json={"status": "removed"})
    resp = client.post(f"/api/portal/spd/interventions/{item['id']}/feedback",
                       headers=world["resident"], json={"feedback": "知道了"})
    assert resp.status_code == 200, resp.text
    after = _mine(client, world, item["id"])
    assert after["read"] is True and after["status"] == "removed"


def test_对照_在管的方案照常标记完成(client, admin, world):
    item = _intervene(client, admin, world, "P267 照常完成")
    resp = client.post(f"/api/portal/spd/interventions/{item['id']}/feedback",
                       headers=world["resident"], json={"feedback": "已按要求执行", "done": True})
    assert resp.status_code == 200 and resp.json()["status"] == "done", resp.text
    assert _mine(client, world, item["id"])["status_name"] == "已完成"


def test_手机页显示后端状态_已移除的不给反馈按钮():
    src = (STATIC / "m" / "m.js").read_text(encoding="utf-8")
    start = src.index("async function renderSpdPlans")
    body = src[start:src.index("\n}\n", start)]
    assert "p.status_name" in body, "干预方案卡片没显示后端给的状态文案"
    assert 'p.status === "removed"' in body, "已移除的方案仍给「标记已读并反馈」按钮"
