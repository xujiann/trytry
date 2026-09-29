"""后端收紧过、页面按钮没跟的两处：死亡收尾移除的复诊与干预照给「恢复」，停用病种的服务申请照给「受理」（P2-796，第二十一批
「页面给出的动作 vs 后端允许的角色与状态」扫描 N4-5；后端一侧是 P2-593 / P2-762）。

P2-593 让档案已结束（死亡 / 迁出 / 排除 / 结案）时一并移除的复诊与干预不能恢复（409「患者已不在管……不能恢复」），P2-762
让停用病种的申请只能驳回（409「……只能驳回」）；页面仍只看状态：移除的一律给「恢复」、待受理的一律给「受理」，点了必 409。
修后清单行出参带 `restorable` / `acceptable`（与写接口同一判据现算），页面按它摆。
"""
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.spd.models import SpdProgram, SpdServiceApply

B = "/api/spd"
PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2796 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    made = {}
    for n, tag in enumerate(("死亡", "在管")):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P2796 {tag}", "id_card": f"33012719700303796{n}"}).json()["id"]
        enrolled = client.post(f"{B}/enrollments", headers=admin, json={
            "patient_id": patient, "program_code": "hypertension", "org_id": org})
        assert enrolled.status_code == 201, enrolled.text
        revisit = client.post(f"{B}/revisits", headers=admin, json={
            "patient_id": patient, "program_code": "hypertension", "plan_date": "2026-12-01"})
        assert revisit.status_code == 201, revisit.text
        intervention = client.post(f"{B}/interventions", headers=admin, json={
            "patient_ids": [patient], "program_code": "hypertension", "content": "低盐饮食", "create_task": False})
        assert intervention.status_code == 201, intervention.text
        made[tag] = {"patient": patient, "enrollment": enrolled.json()["id"], "revisit": revisit.json()["id"],
                     "intervention": intervention.json()["ids"][0]}
    died = client.post(f"{B}/enrollments/{made['死亡']['enrollment']}/lifecycle", headers=admin,
                       json={"event": "death", "reason": "病故"})
    assert died.status_code == 200, died.text   # 收尾一并移除该患者的复诊与干预
    live = made["在管"]
    assert client.patch(f"{B}/revisits/{live['revisit']}", headers=admin, json={"status": "removed"}).status_code == 200
    assert client.patch(f"{B}/interventions/{live['intervention']}", headers=admin,
                        json={"status": "removed"}).status_code == 200   # 在管的患者手工移除：照旧能恢复
    return made


def _row(client, admin, path, patient, row_id):
    return next(r for r in client.get(f"{B}/{path}", headers=admin, params={"patient_id": patient}).json()
                if r["id"] == row_id)


@pytest.mark.parametrize("path", ["revisits", "interventions"])
def test_死亡收尾移除的_清单标不能恢复_与接口一致(client, admin, world, path):
    dead, live = world["死亡"], world["在管"]
    key = path[:-1]
    row = _row(client, admin, path, dead["patient"], dead[key])
    assert (row["status"], row["restorable"]) == ("removed", False), row   # 修前没有这个键，页面照给「恢复」
    got = client.patch(f"{B}/{path}/{dead[key]}", headers=admin, json={"status": "planned"})
    assert got.status_code == 409, got.text
    row = _row(client, admin, path, live["patient"], live[key])
    assert (row["status"], row["restorable"]) == ("removed", True), row
    assert client.patch(f"{B}/{path}/{live[key]}", headers=admin, json={"status": "planned"}).status_code == 200
    assert _row(client, admin, path, live["patient"], live[key])["restorable"] is False   # 不是已移除的，不谈恢复


def test_停用病种的申请_清单标不能受理_与接口一致(client, admin):
    patients = [client.post("/api/patients", headers=admin, json={
        "name": f"P2796 申请人{n}", "id_card": f"33010219500101{2796 + n:04d}"}).json()["id"] for n in (1, 2)]
    with SessionLocal() as db:
        db.add(SpdProgram(code="p2796_off", name="P2796 停用病种", category="chronic", active=True))
        db.add(SpdProgram(code="p2796_on", name="P2796 在用病种", category="chronic", active=True))
        applies = [SpdServiceApply(patient_id=patients[0], program_code="p2796_off", note="想加入", status="pending"),
                   SpdServiceApply(patient_id=patients[1], program_code="p2796_on", note="想加入", status="pending")]
        db.add_all(applies)
        db.commit()
        off, on = (a.id for a in applies)
        db.query(SpdProgram).filter_by(code="p2796_off").update({"active": False})
        db.commit()
    rows = {r["id"]: r for r in client.get(f"{B}/service-applies", headers=admin, params={"limit": 100}).json()}
    assert rows[off]["acceptable"] is False and rows[on]["acceptable"] is True   # 修前没有这个键
    denied = client.post(f"{B}/service-applies/{off}/handle", headers=admin, json={"status": "accepted"})
    assert denied.status_code == 409, denied.text
    ok = client.post(f"{B}/service-applies/{on}/handle", headers=admin, json={"status": "accepted"})
    assert ok.status_code == 200, ok.text


def test_页面按标记摆恢复与受理():
    assert '(i.restorable ? `<button class="btn secondary" data-intv="${i.id}" data-s="planned">恢复</button>` : "—")' in PAGE
    assert '(r.restorable ? `<button class="btn secondary" data-revisit="${r.id}" data-s="planned">恢复</button>` : "—")' in PAGE
    assert '${a.acceptable ? `<button class="btn secondary" data-apply="${a.id}" data-decision="accepted">受理</button>`' in PAGE
