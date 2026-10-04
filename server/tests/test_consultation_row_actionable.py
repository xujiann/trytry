"""远程会诊清单行上的「受理 / 拒绝 / 出意见 / 评价 / 计费」按「这一行当前用户能不能办」摆（P2-1313，第三十八批扫描 AB1-4 的会诊那一半）。

会诊清单（`GET /api/consultations`）是全县最新 200 条（该给谁看随 P1-49 待裁定），页面只看状态摆按钮；五个流转接口都经 `_get`
按所属患者判可见性（`assert_patient_visible`，P0-31）。修前实测（scan38 ab1/r7）：东镇申请、县医院受邀的会诊，与这位患者毫无
关系的西镇医生清单里照样有这一行和「受理」按钮，清单行没有任何 `can_*` 标记，点受理 403「无权调阅该患者档案…」。

修法照 P2-793（`tests/test_row_actionable_flags.py`）：出参补 `can_handle`，后端按 `_get` 的那一句现算（一页的患者合起来判一次：
`visibility.visible_patients_among`，`patient_basis` 的批量布尔版，不留痕），页面按它摆。哪一步该由哪一方做随 P1-71 待裁定、
按角色摆不摆随 P2-447，都不在本条。
"""
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from app.database import SessionLocal, engine
from app.models import AccessLog, ArchiveAuthorization, Consultation, Encounter, FamilyDoctorContract, Patient, User
from app.visibility import clear_visibility_cache, patient_basis, visible_patients_among
from conftest import business_today, login

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

#: 机构 → 与患者「丁」的关系；期望的 can_handle 就是 `_get` 放不放行
ORGS = {
    "a": ("P21313 东镇卫生院", "township", "申请方"),
    "county": ("P21313 县人民医院", "county", "受邀方"),
    "x": ("P21313 西镇卫生院", "township", "毫无关系"),
    "y": ("P21313 南镇卫生院", "township", "本机构看过这位患者的门诊"),
    "z": ("P21313 北镇卫生院", "township", "患者授权本机构调阅（未过期）"),
    "w": ("P21313 中镇卫生院", "township", "授权已过期、另一条已撤销"),
}
EXPECTED = {"a": True, "county": True, "x": False, "y": True, "z": True, "w": False}


@pytest.fixture(scope="module")
def world(client, admin):
    orgs, heads = {}, {}
    for key, (name, level, _why) in ORGS.items():
        resp = client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township" if level == "township" else "lead_hospital", "level": level})
        assert resp.status_code == 201, resp.text
        orgs[key] = resp.json()["id"]
        username = f"p21313_cons_{key}"
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "full_name": username, "role": "doctor", "org_id": orgs[key]})
        assert resp.status_code in (200, 201), resp.text
        heads[key] = login(client, username, "passw0rd1")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21313 会诊患者丁", "id_card": "330106197001016131"}).json()["id"]
    today = business_today()
    with SessionLocal() as db:
        author = db.query(User).filter(User.username == "admin").one().id
        db.add(Encounter(patient_id=patient, org_id=orgs["y"], doctor_name="南镇医生"))
        db.add(ArchiveAuthorization(patient_id=patient, grantee_org_id=orgs["z"], scope="all",
                                    expire_date=(today + timedelta(days=30)).isoformat(), created_by=author))
        db.add(ArchiveAuthorization(patient_id=patient, grantee_org_id=orgs["w"], scope="all",
                                    expire_date=(today - timedelta(days=1)).isoformat(), created_by=author))
        db.add(ArchiveAuthorization(patient_id=patient, grantee_org_id=orgs["w"], scope="all", expire_date="",
                                    status="revoked", created_by=author))
        db.commit()
    clear_visibility_cache()

    def apply(question):
        resp = client.post("/api/consultations", headers=heads["a"], json={
            "patient_id": patient, "from_org_id": orgs["a"], "to_org_id": orgs["county"], "question": question})
        assert resp.status_code == 201, resp.text
        assert resp.json()["can_handle"] is True   # 申请方自己建的单
        return resp.json()["id"]

    applied = apply("胸痛待查")
    done = apply("反复晕厥")
    for path, body in (("accept", {"expert_name": "孙主任"}), ("complete", {"opinion": "建议上转评估起搏器"})):
        resp = client.post(f"/api/consultations/{done}/{path}", headers=heads["county"], json=body)
        assert resp.status_code == 200 and resp.json()["can_handle"] is True, resp.text
    return {"orgs": orgs, "heads": heads, "patient": patient, "applied": applied, "done": done}


def _flags(client, headers, path="/api/consultations") -> dict[int, bool]:
    resp = client.get(path, headers=headers)
    assert resp.status_code == 200, resp.text
    return {c["id"]: c["can_handle"] for c in resp.json()}   # 修前没有这个键


def _logs(patient_id: int) -> int:
    with SessionLocal() as db:
        return db.query(AccessLog).filter(AccessLog.patient_id == patient_id).count()


def test_会诊清单_只有看得了这位患者的机构那一行能办(client, admin, world):
    before = _logs(world["patient"])
    for key, expected in EXPECTED.items():
        flags = _flags(client, world["heads"][key])
        assert {flags[world["applied"]], flags[world["done"]]} == {expected}, (key, ORGS[key][2])
        assert _flags(client, world["heads"][key], "/api/consultations?status=applied")[world["applied"]] is expected, key
    assert set(_flags(client, admin).values()) == {True}   # 全域角色放行
    assert _logs(world["patient"]) == before   # 算标记不是调阅：不按患者筛的清单照旧不留痕


def test_标记与写接口同一口径(client, admin, world):
    """已完成的单点「受理」：`_get` 先判归属再判状态——看不了的 403，看得了的过了归属、卡在状态 409，单子不动。"""
    for key, expected in EXPECTED.items():
        resp = client.post(f"/api/consultations/{world['done']}/accept", headers=world["heads"][key],
                           json={"expert_name": "孙主任"})
        assert resp.status_code == (409 if expected else 403), (key, resp.text)
    resp = client.post(f"/api/consultations/{world['done']}/accept", headers=admin, json={"expert_name": "孙主任"})
    assert resp.status_code == 409, resp.text
    denied = client.post(f"/api/consultations/{world['applied']}/accept", headers=world["heads"]["x"],
                         json={"expert_name": "西镇张医生"})
    assert denied.status_code == 403, denied.text   # 修前西镇清单里这一行照样摆「受理」


def test_批量判据与patient_basis逐个组合一致(client, admin, world):
    """`visible_patients_among` 只给页面判断摆不摆按钮，真正放不放行以 `assert_patient_visible`（`patient_basis`）为准：
    两者在各种依据（就诊、签约含已解约、会诊两方、有效 / 过期 / 撤销授权、毫无关系）× 各类账号下必须逐个一致。"""
    orgs, patient = world["orgs"], world["patient"]
    others = [client.post("/api/patients", headers=admin, json={"name": f"P21313 组合患者{i}", "id_card": id_card}).json()["id"]
              for i, id_card in enumerate(("330106197001016132", "330106197001016133", "330106197001016134"))]
    with SessionLocal() as db:
        author = db.query(User).filter(User.username == "admin").one().id
        db.add(FamilyDoctorContract(patient_id=others[0], org_id=orgs["x"], doctor_name="西镇家医", status="terminated"))
        db.add(FamilyDoctorContract(patient_id=others[1], org_id=orgs["y"], doctor_name="南镇家医"))
        db.add(ArchiveAuthorization(patient_id=others[1], grantee_org_id=orgs["x"], scope="exam", expire_date="",
                                    created_by=author))
        db.commit()
        patients = [patient, *others, 99999999]
        users = [SimpleNamespace(id=-(i + 1), role="doctor", org_id=org) for i, org in enumerate(orgs.values())]
        users += [SimpleNamespace(id=-100, role="admin", org_id=None), SimpleNamespace(id=-101, role="director", org_id=orgs["x"]),
                  SimpleNamespace(id=-102, role="doctor", org_id=None), SimpleNamespace(id=-103, role="custom_x", org_id=orgs["z"])]
        clear_visibility_cache()
        seen = set()
        for user in users:
            expected = {p for p in patients if patient_basis(db, user, p) is not None}
            assert visible_patients_among(db, user, patients) == expected, (user, expected)
            seen.add(frozenset(expected))
        assert visible_patients_among(db, users[0], []) == set()
    assert len(seen) >= 4, "组合太单一：几种依据没能把账号分开，这条一致性就什么也没证明"
    clear_visibility_cache()


def test_页面按行上的标记摆按钮():
    source = (STATIC / "core.js").read_text(encoding="utf-8")
    start = source.index("async function renderConsultations()")
    body = source[start:source.index("\nasync function ", start + 1)]
    assert 'const actions = !c.can_handle ? "—"\n        : c.status === "applied"' in body   # 修前只看 c.status


@contextmanager
def _count_sql():
    counter = {"n": 0}

    def _tick(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _tick)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _tick)


def test_算标记的查询数不随行数涨(client, world):
    """一页的患者合起来判一次：第三家看全县会诊，再多出 10 位与它毫无关系的患者，SQL 条数不变。
    逐行调 `patient_basis` 的话，看不了的结论不进缓存，每多一位就多把三十几张关系表挨个查一遍。"""
    orgs = world["orgs"]

    def listed():
        clear_visibility_cache()
        with _count_sql() as counter:
            resp = client.get("/api/consultations", headers=world["heads"]["x"])
        assert resp.status_code == 200, resp.text
        return counter["n"], len(resp.json())

    before, rows_before = listed()
    with SessionLocal() as db:
        author = db.query(User).filter(User.username == "admin").one().id
        extra = [Patient(name=f"P21313 查询数患者{i}", id_card=f"3301061970010{i + 7000:05d}", ehc_no=f"EHC-P21313-{i}")
                 for i in range(10)]
        db.add_all(extra)
        db.flush()
        db.add_all([Consultation(patient_id=p.id, from_org_id=orgs["a"], to_org_id=orgs["county"], question="P21313 查询数",
                                 created_by=author) for p in extra])
        db.commit()
    after, rows_after = listed()
    assert rows_after == rows_before + 10
    assert after == before, f"查询数随行数增长：{before} -> {after}"
