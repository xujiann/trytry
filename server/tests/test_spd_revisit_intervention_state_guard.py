"""复诊计划与干预按状态机走（P2-593，第十二批「按钮 vs 状态机」扫描 Z2-4）。

两处修改接口原先任意状态改任意状态：已复诊的计划照样给「已复诊」「移除」，再点一次实际复诊日被改成今天；办结了的干预
照样能「办结」「移除」；移除的一律能「恢复」——患者登记死亡、档案收尾时一并移除的复诊与干预恢复回计划中，复诊随后
超期、干预挂在死者名下（档案自己的「恢复管理」对死亡是 409）。

恢复只挡档案已结束（死亡 / 迁出 / 排除 / 结案）的；召回中的、从没建过档的照旧能恢复——召回该不该收工作、死亡该不该
连带别的病种是 P2-509 / P1-112 待裁定的事，这里不替它们定。
"""
from pathlib import Path

import pytest

B = "/api/spd"
PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2593 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    made = {}
    for n, tag in enumerate(("办结", "死亡", "在管", "未建档")):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P2593 {tag}", "id_card": f"33012719700303593{n}"}).json()["id"]
        enrollment = None
        if tag != "未建档":
            enrolled = client.post(f"{B}/enrollments", headers=admin, json={
                "patient_id": patient, "program_code": "hypertension", "org_id": org})
            assert enrolled.status_code == 201, enrolled.text
            enrollment = enrolled.json()["id"]
        revisit = client.post(f"{B}/revisits", headers=admin, json={
            "patient_id": patient, "program_code": "hypertension", "plan_date": "2026-12-01"})
        assert revisit.status_code == 201, revisit.text
        intervention = client.post(f"{B}/interventions", headers=admin, json={
            "patient_ids": [patient], "program_code": "hypertension", "content": "低盐饮食", "create_task": False})
        assert intervention.status_code == 201, intervention.text
        made[tag] = {"enrollment": enrollment, "revisit": revisit.json()["id"],
                     "intervention": intervention.json()["ids"][0]}
    died = client.post(f"{B}/enrollments/{made['死亡']['enrollment']}/lifecycle", headers=admin, json={
        "event": "death", "reason": "病故"})
    assert died.status_code == 200, died.text
    assert died.json()["closed"]["revisits"] == 1 and died.json()["closed"]["interventions"] == 1   # 收尾一并移除
    return made


def _revisit(client, admin, revisit_id, **body):
    return client.patch(f"{B}/revisits/{revisit_id}", headers=admin, json=body)


def _intervention(client, admin, intervention_id, **body):
    return client.patch(f"{B}/interventions/{intervention_id}", headers=admin, json=body)


def test_已复诊的计划不能再改状态(client, admin, world):
    revisit = world["办结"]["revisit"]
    done = _revisit(client, admin, revisit, status="done", actual_date="2026-11-28")
    assert done.status_code == 200, done.text
    for body in ({"status": "done", "actual_date": "2026-12-05"}, {"status": "removed"}, {"status": "planned"}):
        got = _revisit(client, admin, revisit, **body)
        assert (got.status_code, got.json()["detail"]) == (409, "已复诊的计划不能再改状态"), body   # 修前 200
    row = next(r for r in client.get(f"{B}/revisits", params={"patient_id": done.json()["patient_id"]},
                                     headers=admin).json() if r["id"] == revisit)
    assert (row["status"], row["actual_date"]) == ("done", "2026-11-28")   # 修前实际复诊日被改成 12-05


def test_办结的干预不能再改状态_反馈照旧能记(client, admin, world):
    intervention = world["办结"]["intervention"]
    assert _intervention(client, admin, intervention, status="done").status_code == 200
    for status in ("removed", "planned", "done"):
        got = _intervention(client, admin, intervention, status=status)
        assert (got.status_code, got.json()["detail"]) == (409, "干预已办结，不能再改状态"), status   # 修前 200
    noted = _intervention(client, admin, intervention, feedback="血压平稳")
    assert noted.status_code == 200 and noted.json()["status"] == "done" and noted.json()["feedback"] == "血压平稳"


def test_死亡收尾移除的复诊与干预不能恢复(client, admin, world):
    got = _revisit(client, admin, world["死亡"]["revisit"], status="planned")
    assert (got.status_code, got.json()["detail"]) == (409, "患者已不在管（已死亡），复诊计划不能恢复")   # 修前 200
    got = _intervention(client, admin, world["死亡"]["intervention"], status="planned")
    assert (got.status_code, got.json()["detail"]) == (409, "患者已不在管（已死亡），干预不能恢复")   # 修前 200


@pytest.mark.parametrize("tag", ["在管", "未建档"])
def test_手工移除的照旧能恢复(client, admin, world, tag):
    for patch, key in ((_revisit, "revisit"), (_intervention, "intervention")):
        assert patch(client, admin, world[tag][key], status="removed").status_code == 200
        restored = patch(client, admin, world[tag][key], status="planned")
        assert restored.status_code == 200 and restored.json()["status"] == "planned", restored.text


def test_清单只在没办完的行上给动作按钮():
    revisit_row = PAGE[PAGE.index("function spdRevisitTable(rows) {"):]
    assert ': r.status === "done" ? "—"' in revisit_row[:revisit_row.index("data-s=\"done\">已复诊")]   # 修前照给「已复诊」「移除」
    intervention_row = PAGE[PAGE.index('data-intv="${i.id}" data-s="planned">恢复'):]
    assert intervention_row.index(': i.status === "done" ? "—"') < intervention_row.index('data-s="done">办结')
