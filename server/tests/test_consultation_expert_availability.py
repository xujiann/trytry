"""会诊专家库的「排班状态」建档后改得了：暂停 / 恢复排班（P2-1302，第三十八批扫描 AB2-4）。

修前专家只有 `POST /api/consultations/experts`（建档）与 `GET`（清单）两个接口，PATCH / PUT / DELETE 一律 404。受理时
`_check_expert` 按排班状态拦（P2-764，注释写着「页面打开之后专家被设成暂停排班」，前提是这个状态会变），可这个状态建档后
就改不了：专家请假暂停不了，受理照样选他（实测 200 accepted）；建档时误选「暂停排班」的永远受理不了（409「该专家已暂停排班」），
同名重建又是 409「专家已存在」（专家姓名唯一）。页面上的专家表只有「排班状态」一列，没有切换按钮。

修法：加 `PATCH /api/consultations/experts/{expert_id}`，请求体只收 `available`（必填，送目标状态而不是「切换」），权限与机构
口径照抄同文件的建档（`require_admin` + `assert_org_writable`）；页面专家表每行给「暂停排班 / 恢复排班」（仅管理员可见），
暂停先在页内确认框里确认。
"""
from pathlib import Path

import pytest

from conftest import login

from app.database import SessionLocal
from app.models import ConsultExpert

import test_frontend_api_calls_resolve as resolve

B = "/api/consultations"
STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": f"P21302 {name}", "org_type": otype, "level": level}).json()["id"]
        for name, otype, level in (("申请卫生院", "township", "township"), ("受邀县医院", "lead_hospital", "county"))]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21302 会诊患者", "id_card": "330106197003031302"}).json()["id"]
    heads = {}
    for role in ("doctor", "director"):
        username = f"p21302_{role}"
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "full_name": username, "role": role, "org_id": orgs[1]})
        assert created.status_code in (200, 201), created.text
        heads[role] = login(client, username, "passw0rd1")
    return {"from": orgs[0], "to": orgs[1], "patient": patient, "heads": heads}


def _expert(client, admin, world, name, available=True):
    created = client.post(f"{B}/experts", headers=admin, json={
        "name": name, "org_id": world["to"], "specialty": "心内科", "available": available})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _consultation(client, admin, world):
    created = client.post(B, headers=admin, json={
        "patient_id": world["patient"], "from_org_id": world["from"], "to_org_id": world["to"], "question": "P21302 会诊"})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _set(client, headers, expert_id, body):
    return client.patch(f"{B}/experts/{expert_id}", headers=headers, json=body)


def _available(expert_id):
    with SessionLocal() as db:
        return db.get(ConsultExpert, expert_id).available


def test_暂停排班后受理选他409_恢复后照常受理(client, admin, world):
    """专家请假：修前暂停不了（PATCH 404），受理照样选他（200）。另留一位可排班的专家——库里一位可排班的都没有时，
    受理按 P2-764 退回手填、不按排班状态拦，那是另一条规矩。"""
    qian = _expert(client, admin, world, "P21302 钱专家")
    _expert(client, admin, world, "P21302 赵专家")
    paused = _set(client, admin, qian, {"available": False})
    assert paused.status_code == 200, paused.text   # 修前 404：专家只有 POST / GET
    assert paused.json() == {"id": qian, "name": "P21302 钱专家", "org_id": world["to"], "specialty": "心内科",
                             "available": False}
    listed = {e["id"]: e for e in client.get(f"{B}/experts", headers=admin).json()}
    assert listed[qian] == paused.json()   # 与清单同形

    cid = _consultation(client, admin, world)
    refused = client.post(f"{B}/{cid}/accept", headers=admin, json={"expert_name": "P21302 钱专家"})
    assert refused.status_code == 409, refused.text   # 修前 200 accepted：请假的专家照样被选去受理
    assert refused.json()["detail"] == "该专家已暂停排班，不能受理"

    again = _set(client, admin, qian, {"available": False})   # 旧页面上再点一次暂停：仍是暂停，不会被翻回去
    assert again.status_code == 200 and again.json()["available"] is False, again.text

    resumed = _set(client, admin, qian, {"available": True})
    assert resumed.status_code == 200 and resumed.json()["available"] is True, resumed.text
    accepted = client.post(f"{B}/{cid}/accept", headers=admin, json={"expert_name": "P21302 钱专家"})
    assert accepted.status_code == 200, accepted.text
    assert (accepted.json()["status"], accepted.json()["expert_name"]) == ("accepted", "P21302 钱专家")


def test_建档误选暂停排班的_恢复后受理得了_不必同名重建(client, admin, world):
    """修前：建档误选「暂停排班」的永远受理不了，同名重建 409「专家已存在」——这位专家在系统里就废了。"""
    sun = _expert(client, admin, world, "P21302 孙专家", available=False)
    cid = _consultation(client, admin, world)
    assert client.post(f"{B}/{cid}/accept", headers=admin, json={"expert_name": "P21302 孙专家"}).status_code == 409
    rebuilt = client.post(f"{B}/experts", headers=admin, json={"name": "P21302 孙专家", "org_id": world["to"]})
    assert rebuilt.status_code == 409 and rebuilt.json()["detail"] == "专家已存在", rebuilt.text   # 重建这条路本来就不通

    assert _set(client, admin, sun, {"available": True}).status_code == 200   # 修前 404
    accepted = client.post(f"{B}/{cid}/accept", headers=admin, json={"expert_name": "P21302 孙专家"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["expert_name"] == "P21302 孙专家"


def test_改排班只限管理员_不存在的404_不送available的422(client, admin, world):
    li = _expert(client, admin, world, "P21302 李专家")
    for role in ("doctor", "director"):   # 本机构医师、全域角色的院长都不行：与建档同一个 require_admin
        denied = _set(client, world["heads"][role], li, {"available": False})
        assert denied.status_code == 403, (role, denied.text)
        assert denied.json()["detail"] == "需要管理员权限"
    assert _available(li) is True

    missing = _set(client, admin, 99999999, {"available": False})
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"] == "专家不存在"

    for body in ({}, {"name": "P21302 改名"}, {"available": None}):   # 必填、只收排班状态：不替人猜是暂停还是恢复
        rejected = _set(client, admin, li, body)
        assert rejected.status_code == 422, (body, rejected.text)
    assert _available(li) is True


def _render_consultations() -> str:
    source = (STATIC / "core.js").read_text(encoding="utf-8")
    start = source.index("async function renderConsultations()")
    return source[start:source.index("\nasync function ", start + 1)]


def test_专家表每行有暂停恢复排班按钮_调的是这个接口():
    body = _render_consultations()
    table = body[body.index('table(["ID", "姓名", "机构", "专业方向", "排班状态"]'):]
    table = table[:table.index("</tr>`)")]
    assert '.concat(canExpert ? ["操作"] : [])' in table   # 按钮只摆给管理员（后端 require_admin）
    assert '<button class="btn danger" data-act="expert-off" data-id="${x.id}">暂停排班</button>' in table   # 修前没有
    assert '<button class="btn secondary" data-act="expert-on" data-id="${x.id}">恢复排班</button>' in table
    assert table.index("${canExpert ?") < table.index('data-act="expert-off"')

    handler = body[body.index('} else if (act === "expert-off" || act === "expert-on") {'):]
    handler = handler[:handler.index("route();")]
    assert 'spdModal("暂停排班", [], {' in handler   # 暂停先在页内框里确认（同页受理 / 计费都是页内框）
    assert "prompt(" not in handler and "confirm(" not in handler
    assert ("api(`/api/consultations/experts/${id}`, "
            '{ method: "PATCH", body: JSON.stringify({ available: !off }) });') in handler
    # 同一把尺子（动词级孤儿棘轮用的调用点解析）认得出这是对这条路由的 PATCH
    assert ("PATCH", "/api/consultations/experts/{expert_id}") in set(resolve.scan()["calls"])
