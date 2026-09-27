"""已派单的上门工单能从页面上取消（P2-595，第十二批「按钮 vs 状态机」扫描 Z2-6）。

取消接口只挡已完成的（`homevisits.cancel_visit`），页面原先只给待派单的行画「取消」、已派单的只给「完成」——派出去
才知道去不成（住院了、搬走了）的工单就一直挂在待完成里，只能硬点「完成」编一段服务记录。取消照旧只给经办 / 医师
（P2-429），公卫人员在已派单的行上只见「完成」。
"""
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-public.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def order(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2595 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2595 患者", "id_card": "330127196808082595"}).json()["id"]
    created = client.post("/api/homevisits", headers=admin, json={
        "patient_id": patient, "org_id": org, "service_type": "nursing", "demand": "换药"})
    assert created.status_code == 201, created.text
    dispatched = client.post(f"/api/homevisits/{created.json()['id']}/dispatch", headers=admin,
                             json={"assignee_name": "护士王"})
    assert dispatched.status_code == 200 and dispatched.json()["status"] == "dispatched", dispatched.text
    return created.json()["id"]


def test_已派单的工单接口照收取消_取消后不能再完成(client, admin, order):
    cancelled = client.post(f"/api/homevisits/{order}/cancel", headers=admin)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled", cancelled.text
    done = client.post(f"/api/homevisits/{order}/complete", headers=admin, json={"service_note": "补记"})
    assert done.status_code == 409, done.text


def test_页面给已派单的行也画取消_仍只给能取消的角色():
    start = PAGE.index("async function drawHomeVisits() {")
    body = PAGE[start:PAGE.index("/* ---------------- 启动", start)]
    assert 'const cancel = canDispatch ? `<button class="btn danger" data-hvcancel="${o.id}">取消</button>` : "";' in body
    dispatched = body[body.index('o.status === "dispatched" && canComplete'):]
    assert dispatched.index("${cancel}") < dispatched.index('"—"')   # 修前这一支只有「完成」
