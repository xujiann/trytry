"""患者死亡 / 迁出 / 排除时结案不动随访记录：死者名下的随访照旧到期、超期，挂在随访清单与超期数里（P1-129）。

`close_open_work`（死亡 / 迁出 / 排除 / 召回四种生命周期事件共用）终止这名患者在该病种下的任务、路径、干预与复诊——
随访记录不在其中：计划好的随访照旧到期，被超期扫描置为已超期，排在随访清单、工作台超期数与外呼里。待裁定事项清单
P1-108 一节还把它当成了「死亡 / 迁出 / 排除时置 removed」。

修法：结案一并移除该病种在本机构（或没挂机构）的未完成随访（待随访 / 已超期），与手工「移除」同一个状态（可手工恢复）；
已完成、失访的是留痕，不动；别的病种、别家机构的不动（迁入确认时目标机构的随访不能被原档案的结案带走）。回执 `closed`
多一项 `followups` 计数（只加键）。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": f"P129 {name}", "org_type": "township", "level": "township"}).json()["id"] for name in ("本院", "别院")]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P129 患者", "id_card": "330127196905050129"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": orgs[0]})
    assert enrollment.status_code == 201, enrollment.text
    return {"org": orgs[0], "other_org": orgs[1], "patient": patient, "enrollment": enrollment.json()["id"]}


def _record(world, **kw):
    from app.database import SessionLocal
    from app.spd.models import SpdFollowupRecord

    fields = {"patient_id": world["patient"], "program_code": "hypertension", "org_id": world["org"],
              "planned_at": "2099-01-01", "status": "planned", **kw}
    with SessionLocal() as db:
        row = SpdFollowupRecord(**fields)
        db.add(row)
        db.commit()
        return row.id


def test_死亡结案_本病种本机构未完成的随访一并移除_留痕与别处的不动(client, admin, world):
    from app.database import SessionLocal
    from app.spd.models import SpdFollowupRecord

    ids = {
        "planned": _record(world),
        "overdue": _record(world, planned_at="2020-01-01", status="overdue"),
        "no_org": _record(world, org_id=None),
        "done": _record(world, planned_at="2020-02-01", status="done", executed_at="2020-02-01"),
        "unreachable": _record(world, planned_at="2020-03-01", status="unreachable"),
        "other_program": _record(world, program_code="diabetes"),
        "other_org": _record(world, org_id=world["other_org"]),
    }
    resp = client.post(f"{B}/enrollments/{world['enrollment']}/lifecycle", headers=admin,
                       json={"event": "death", "reason": "病故"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["closed"]["followups"] == 3, resp.json()["closed"]   # 修前没有这一项，三条都还挂着
    with SessionLocal() as db:
        statuses = {key: db.get(SpdFollowupRecord, row_id).status for key, row_id in ids.items()}
    assert statuses == {"planned": "removed", "overdue": "removed", "no_org": "removed", "done": "done",
                        "unreachable": "unreachable", "other_program": "planned", "other_org": "planned"}
    # 与手工移除同一个状态：错移了可以手工恢复
    restored = client.patch(f"{B}/followup-records/{ids['planned']}", headers=admin, json={"status": "planned"})
    assert restored.status_code == 200 and restored.json()["status"] == "planned", restored.text
