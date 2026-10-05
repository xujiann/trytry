"""在线咨询回复挂处方：已退药冲销的处方回 409（P2-1476，第四十三批「远程会诊与互联网诊疗」扫描 AG2-1 冲销那一半）。

退药冲销只把发药记录置 reversed、药回库房，处方表不动（它的状态是审方结论）；`dispense.reverse_dispense` 写明冲销后的
处方不可再发（唯一约束仍占着）、确需再发的走新处方。在线咨询的回复原先只看处方同一患者、审方结论在系统审通过 / 药师审通过
之内：修前实测（scan43 ag2/r2）「C2 续方回复关联已冲销处方: 200 status=replied prescription_id=2」——咨询显示「已回复」、
写着同意续方，挂上的处方却永远发不出去，患者拿去药房是 409「该处方已发药，不可重复发药」。

修后挂冲销过的处方回 409「该处方已退药冲销，续方请开新处方」，咨询仍是待回复；判据复用用量统计、医生 360 的那一句
（`dispense.prescription_not_reversed`，P2-624 / P2-1198），不另写。出参只在末尾加两个键——关联处方的审方状态中文名、是否已
退药冲销（与 360 处方段的 `status_name` / `dispense_reversed` 同义）——页面「关联处方」一列带出来：回复之后才退药的那张，
回复时挡不住，清单上也看得出来。

不拦的（业务口径，随 P2-900 待裁定）：咨询之前就已发过药的旧处方、普通咨询（consult_type=consult）挂处方。
"""
import os

import pytest

from app.database import SessionLocal
from app.models import OnlineConsult
from conftest import login

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
#: 修前出参的键与次序（P2-1476 只在末尾加键）
ORIGINAL_KEYS = ["patient_id", "org_id", "consult_type", "question", "id", "reply", "doctor_name", "status",
                 "prescription_id"]
NEW_KEYS = ["prescription_status_name", "prescription_dispense_reversed"]


def _ok(resp):
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


@pytest.fixture(scope="module")
def world(client, admin):
    org = _ok(client.post("/api/organizations", headers=admin, json={
        "name": "P21476 卫生院", "org_type": "township", "level": "township"}))["id"]
    _ok(client.post("/api/users", headers=admin, json={
        "username": "p21476_doc", "password": "passw0rd1", "full_name": "P21476 全科医生", "role": "doctor",
        "org_id": org}))
    patient = _ok(client.post("/api/patients", headers=admin, json={
        "name": "P21476 续方患者", "id_card": "330106196501011476"}))["id"]
    _ok(client.post("/api/pharmacy/stocks", headers=admin, json={
        "org_id": org, "drug_code": "P21476-RAM", "drug_name": "雷米普利片", "quantity": 1000}))
    return {"org": org, "patient": patient, "doctor": login(client, "p21476_doc", "passw0rd1")}


def _prescribe(client, admin, world):
    rx = _ok(client.post("/api/prescriptions", headers=admin, json={
        "patient_id": world["patient"], "org_id": world["org"], "diagnosis_name": "高血压",
        "items": [{"drug_code": "P21476-RAM", "drug_name": "雷米普利片", "daily_dose": 5, "days": 30}]}))
    assert rx["status"] == "auto_passed", rx
    return rx["id"]


def _dispense_then_reverse(client, admin, rx_id):
    dispensed = _ok(client.post("/api/dispense", headers=admin, json={"prescription_id": rx_id}))
    _ok(client.post(f"/api/dispense/{dispensed['id']}/reverse", headers=admin, json={"reason": "患者退药"}))
    again = client.post("/api/dispense", headers=admin, json={"prescription_id": rx_id})
    assert again.status_code == 409, again.text   # 冲销后的处方不可再发（reverse_dispense 自己写明的规矩）


def _consult(client, admin, world):
    return _ok(client.post("/api/telemedicine/consults", headers=admin, json={
        "patient_id": world["patient"], "org_id": world["org"], "consult_type": "repeat_rx",
        "question": "血压平稳，申请续方"}))["id"]


def _reply(client, world, consult_id, rx_id):
    return client.post(f"/api/telemedicine/consults/{consult_id}/reply", headers=world["doctor"], json={
        "reply": "同意续方 30 天", "doctor_name": "P21476 全科医生", "prescription_id": rx_id})


def test_挂退药冲销过的处方_409_咨询仍是待回复(client, admin, world):
    rx_id = _prescribe(client, admin, world)
    _dispense_then_reverse(client, admin, rx_id)
    consult_id = _consult(client, admin, world)
    resp = _reply(client, world, consult_id, rx_id)
    assert resp.status_code == 409, resp.text   # 修前 200、显示「已回复」，这张方却永远发不出去
    assert resp.json()["detail"] == "该处方已退药冲销，续方请开新处方"
    with SessionLocal() as db:
        row = db.get(OnlineConsult, consult_id)
        assert (row.status, row.prescription_id, row.reply, row.doctor_name) == ("open", None, "", "")


def test_挂一张正常已审的处方照旧200(client, admin, world):
    rx_id = _prescribe(client, admin, world)
    consult_id = _consult(client, admin, world)
    resp = _reply(client, world, consult_id, rx_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["status"], body["prescription_id"]) == ("replied", rx_id)
    assert list(body) == ORIGINAL_KEYS + NEW_KEYS   # 原有键与次序不动，新键在末尾
    assert (body["prescription_status_name"], body["prescription_dispense_reversed"]) == ("系统审通过", False)


def test_回复之后才退药的_清单那一行看得出已退药冲销(client, admin, world):
    rx_id = _prescribe(client, admin, world)
    linked = _consult(client, admin, world)
    assert _reply(client, world, linked, rx_id).status_code == 200
    _dispense_then_reverse(client, admin, rx_id)   # 回复时还没退药，挡不住；清单上要看得出来
    made = _ok(client.post("/api/telemedicine/consults", headers=admin, json={
        "patient_id": world["patient"], "org_id": world["org"], "question": "最近头晕怎么办"}))
    assert list(made) == ORIGINAL_KEYS + NEW_KEYS   # 建单出参同一形状
    bare = made["id"]
    rows = {c["id"]: c for c in _ok(client.get("/api/telemedicine/consults", headers=admin))}
    assert list(rows[linked]) == ORIGINAL_KEYS + NEW_KEYS
    assert (rows[linked]["prescription_id"], rows[linked]["prescription_status_name"],
            rows[linked]["prescription_dispense_reversed"]) == (rx_id, "系统审通过", True)   # 审方结论不变，另标已退药
    assert (rows[bare]["prescription_id"], rows[bare]["prescription_status_name"],
            rows[bare]["prescription_dispense_reversed"]) == (None, None, False)   # 没关联处方的
    # 单条出参（回复 / 结束）与清单同一形状
    got = _ok(client.post(f"/api/telemedicine/consults/{bare}/reply", headers=world["doctor"],
                          json={"reply": "按原方案服药", "doctor_name": "P21476 全科医生"}))
    assert list(got) == ORIGINAL_KEYS + NEW_KEYS
    assert (got["prescription_status_name"], got["prescription_dispense_reversed"]) == (None, False)
    closed = _ok(client.post(f"/api/telemedicine/consults/{linked}/close", headers=admin))
    assert (closed["status"], closed["prescription_status_name"], closed["prescription_dispense_reversed"]) == (
        "closed", "系统审通过", True)


def test_页面关联处方一列带出处方状态():
    with open(os.path.join(STATIC, "pages-clinical.js"), encoding="utf-8") as fh:
        source = fh.read()
    start = source.index("async function renderTelemedicine()")
    body = source[start:source.index("\nasync function ", start + 1)]
    assert "<td>${c.prescription_id ?? \"—\"}</td>" not in body   # 修前只印编号
    assert "esc(c.prescription_status_name)" in body and "c.prescription_dispense_reversed" in body
    assert "已退药冲销" in body
