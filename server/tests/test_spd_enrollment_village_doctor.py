"""签约纳管：签约村医没填、操作人自己是在用的村医的，记成操作人；页面两处表单补上「签约村医」（P1-217，第二十批「同一个数，
多处口径」扫描 M1-1）。

签约计分早写明「谁签的谁得分，村医字段没填就按操作人算」，移动端「签约居民」、考核「村医签约数」（vd_sign）与随访完成
积分却都按档案上的 `village_doctor_id` 数——而页面的签约表单与调整档案弹窗根本没有这个字段。实测修前：村医照页面表单
签约 3 户，积分余额 15（签约分照记），档案上的签约村医全是空：村医方案跑分 vd_sign 取数 enrolled 0、得分 0；办结一条
随访任务，随访分一分不记（`award_points(None)`）。演示种子走接口写了这个字段，所以演示环境看不出来。
"""
import re
from pathlib import Path

import pytest

from conftest import login

from app.database import SessionLocal
from app.spd.models import SpdEnrollment, SpdPointAccount, SpdPointRecord, SpdVillageDoctor

B = "/api/spd"
PAGES_SPD = Path(__file__).resolve().parent.parent / "app" / "static" / "pages-spd.js"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1217 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    ids = {}
    for username in ("p1217_vd", "p1217_vd2", "p1217_off", "p1217_doc"):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "full_name": username, "role": "doctor", "org_id": org})
        assert created.status_code in (200, 201), created.text
        ids[username] = created.json()["id"]
    for username in ("p1217_vd", "p1217_vd2", "p1217_off"):
        resp = client.post(f"{B}/village-doctors", headers=admin, json={
            "user_id": ids[username], "org_id": org, "village": "P1217 村"})
        assert resp.status_code == 201, resp.text
    with SessionLocal() as db:   # 村医档案停用了的账号
        db.query(SpdVillageDoctor).filter_by(user_id=ids["p1217_off"]).update({"active": False})
        db.commit()
    return {"org": org, "ids": ids, "n": 0,
            **{name: login(client, name, "passw0rd1") for name in ("p1217_vd", "p1217_off", "p1217_doc")}}


def _enroll(client, admin, world, who, **extra):
    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P1217 患者{world['n']}", "id_card": f"33010219500101{1216 + world['n']:04d}"}).json()["id"]
    client.post("/api/encounters", headers=world[who], json={"patient_id": patient, "org_id": world["org"],
                                                              "diagnosis_name": "高血压"})
    resp = client.post(f"{B}/enrollments", headers=world[who], json={
        "patient_id": patient, "program_code": "hypertension", "org_id": world["org"], **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_村医照页面表单签约_档案记本人_随访积分记到本人(client, admin, world):
    vd = world["ids"]["p1217_vd"]
    enrollment = _enroll(client, admin, world, "p1217_vd")
    assert enrollment["village_doctor_id"] == vd   # 修前 None：签约居民、村医签约数都数成 0
    task = client.post(f"{B}/tasks", headers=world["p1217_vd"], json={
        "patient_id": enrollment["patient_id"], "program_code": "hypertension", "task_type": "followup",
        "title": "季度随访", "org_id": world["org"]})
    assert task.status_code == 201, task.text
    done = client.post(f"{B}/tasks/{task.json()['id']}/complete", headers=world["p1217_vd"], json={"result": {"note": "已随访"}})
    assert done.status_code == 200, done.text
    with SessionLocal() as db:
        kinds = sorted(code for (code,) in db.query(SpdPointRecord.rule_code)
                       .join(SpdPointAccount, SpdPointAccount.id == SpdPointRecord.account_id)
                       .filter(SpdPointAccount.user_id == vd))
    assert {"pt_sign", "pt_followup"} <= set(kinds), kinds   # 修前随访分一分不记


def test_填了签约村医的照填的记(client, admin, world):
    other = world["ids"]["p1217_vd2"]
    assert _enroll(client, admin, world, "p1217_vd", village_doctor_id=other)["village_doctor_id"] == other


@pytest.mark.parametrize("who", ["p1217_doc", "p1217_off"], ids=["不是村医", "村医档案已停用"])
def test_操作人不是在用的村医_照旧留空(client, admin, world, who):
    enrollment = _enroll(client, admin, world, who)
    assert enrollment["village_doctor_id"] is None
    with SessionLocal() as db:
        assert db.get(SpdEnrollment, enrollment["id"]).village_doctor_id is None


def test_签约表单与调整档案弹窗都有签约村医():
    source = PAGES_SPD.read_text(encoding="utf-8")
    form = source[source.index('<form class="inline" id="spd-enroll-form">'):]
    form = form[:form.index("</form>")]
    assert 'name="village_doctor_id"' in form   # 修前没有这个输入框
    assert re.search(r'formJson\(e\.target, \[[^\]]*"village_doctor_id"', source)
    modal = source[source.index('spdModal("调整纳管档案'):]
    modal = modal[:modal.index("postAction(")]
    assert '{ name: "village_doctor_id"' in modal and '"manager_user_id", "village_doctor_id"]' in modal
