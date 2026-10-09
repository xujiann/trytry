"""慢专病在线咨询清单按患者筛（P2-1604，第四十七批「慢专病服务域」扫描 AK2-5）。

`GET /api/spd/consults` 原先不收 `patient_id`，调的是 `scope_patient_list(..., None, ...)`——`?patient_id=` 被静默
忽略、照回全部。修前实测（scan47 ak2/r4）：三位患者各一条会话，`?patient_id=1` 三条全回 `[(3,3),(2,2),(1,1)]`。
个案管理师页只取最新 50 条加进行中的 200 条，已结束的咨询一被新会话挤出窗口，界面上就找不回了（需求对照表个案管理师端
#6「患者会话查询」）。

修法照同文件干预 / 复诊 / 评估清单与互联网诊疗咨询清单（P2-1477）：把 `patient_id` 交给 `scope_patient_list`——
先判这位患者看不看得（看不见 403、不留痕）并留痕（资源名 spd_consult），再过滤；不带患者号照旧按可见患者收口、不留痕。
页面咨询面板补「按患者查」，查询失败只在这一段报错。
"""
import os

import pytest

from app.database import SessionLocal
from app.models import AccessLog
from app.spd.models import SpdConsult
from conftest import login

LIST = "/api/spd/consults"
PAGE = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "pages-spd.js"), encoding="utf-8").read()


@pytest.fixture(scope="module")
def world(client, admin):
    """甲卫生院在管三位高血压患者，各一条会话；患者一那条已结束，之后患者二又来了 60 条已结束的会话（把它挤出最新 50 条）；
    丙卫生院与三位患者都没有关系。"""
    orgs = {}
    for key in ("a", "c"):
        resp = client.post("/api/organizations", headers=admin, json={
            "name": f"P21604 {key}卫生院", "org_type": "township", "level": "township"})
        assert resp.status_code == 201, resp.text
        orgs[key] = resp.json()["id"]
    heads, doctors = {}, {}
    for key in ("a", "c"):
        username = f"p21604_{key}"
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "full_name": username, "role": "doctor", "org_id": orgs[key]})
        assert resp.status_code in (200, 201), resp.text
        doctors[key] = resp.json()["id"]
        heads[key] = login(client, username, "passw0rd1")
    patients = []
    for i in range(3):
        resp = client.post("/api/patients", headers=admin, json={
            "name": f"P21604 患者{i}", "id_card": f"33010619600101{1604 + i:04d}"})
        assert resp.status_code in (200, 201), resp.text
        patients.append(resp.json()["id"])
        resp = client.post("/api/spd/enrollments", headers=admin, json={
            "patient_id": patients[-1], "program_code": "hypertension", "org_id": orgs["a"],
            "doctor_user_id": doctors["a"]})
        assert resp.status_code == 201, resp.text
    with SessionLocal() as db:
        consults = []
        for i, pid in enumerate(patients):
            row = SpdConsult(patient_id=pid, program_code="hypertension", doctor_id=doctors["a"],
                             status="closed" if i == 0 else "open")
            db.add(row)
            db.flush()
            consults.append(row.id)
        db.add_all([SpdConsult(patient_id=patients[1], program_code="hypertension", doctor_id=doctors["a"],
                               status="closed") for _ in range(60)])
        db.commit()
    return {"heads": heads, "patients": patients, "consults": consults}


def _logs(patient_id: int) -> int:
    with SessionLocal() as db:
        return db.query(AccessLog).filter(AccessLog.patient_id == patient_id,
                                          AccessLog.resource == "spd_consult").count()


def test_按患者筛只回这位患者的会话并留痕(client, world):
    target = world["patients"][0]
    before = _logs(target)
    resp = client.get(LIST, headers=world["heads"]["a"], params={"patient_id": target})
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    # 修前参数被忽略，三位患者的会话（连同后来的 60 条）全回
    assert [(r["id"], r["patient_id"], r["status"]) for r in rows] == [(world["consults"][0], target, "closed")]
    assert resp.headers["X-Total-Count"] == "1"
    assert _logs(target) == before + 1   # 按患者筛即调阅这位患者的咨询，留痕


def test_被挤出最新一页的已结束会话_按患者筛找得回(client, world):
    latest = client.get(LIST, headers=world["heads"]["a"], params={"limit": 50})
    assert latest.status_code == 200, latest.text
    assert world["consults"][0] not in {r["id"] for r in latest.json()}   # 页面默认窗口里已经没有它
    resp = client.get(LIST, headers=world["heads"]["a"], params={"patient_id": world["patients"][0], "limit": 50})
    assert [r["id"] for r in resp.json()] == [world["consults"][0]]


def test_按患者筛与状态筛可以叠加(client, world):
    params = {"patient_id": world["patients"][2], "status": "open"}
    assert [r["id"] for r in client.get(LIST, headers=world["heads"]["a"], params=params).json()] == [world["consults"][2]]
    params["status"] = "closed"
    assert client.get(LIST, headers=world["heads"]["a"], params=params).json() == []


def test_与患者无关的机构按患者筛_403_不留痕(client, world):
    target = world["patients"][0]
    before = _logs(target)
    resp = client.get(LIST, headers=world["heads"]["c"], params={"patient_id": target})
    assert resp.status_code == 403, resp.text[:200]   # 修前参数被忽略、200
    assert _logs(target) == before


def test_不带患者号照旧按可见患者收口_不留痕(client, admin, world):
    with SessionLocal() as db:
        newest = [cid for (cid,) in db.query(SpdConsult.id).filter(SpdConsult.patient_id.in_(world["patients"]))
                  .order_by(SpdConsult.id.desc()).limit(100)]
        logs_before = db.query(AccessLog).filter(AccessLog.resource == "spd_consult").count()
    resp = client.get(LIST, headers=world["heads"]["a"])
    assert resp.status_code == 200, resp.text
    assert [r["id"] for r in resp.json()] == newest
    assert resp.headers["X-Total-Count"] == str(3 + 60)
    assert client.get(LIST, headers=world["heads"]["c"]).json() == []   # 无关机构照旧一条看不见
    with SessionLocal() as db:
        assert db.query(AccessLog).filter(AccessLog.resource == "spd_consult").count() == logs_before


def test_页面咨询面板有按患者查_失败只在这一段报错():
    start = PAGE.index("async function renderSpdManager()")
    body = PAGE[start:PAGE.index("\nfunction ", start)]
    assert 'id="spd-consult-filter"' in body and '<input name="patient_id"' in body[body.index('id="spd-consult-filter"'):]
    assert '<div id="spd-consult-list">${spdConsultTable(consults)}</div>' in body
    handler = body[body.index('$("#spd-consult-filter").onsubmit'):]
    handler = handler[:handler.index("\n  };")]
    assert "/api/spd/consults?patient_id=${encodeURIComponent(pid)}" in handler
    # 查询失败写在清单这一段，不让 api() 的异常掀掉整页
    assert 'catch (err) { $("#spd-consult-list").innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }' in handler
