"""知情同意「撤回」与更正 / 注销「审核」的归属校验与留痕（P0-28）。

2026-09-24 逐条判 P1-71 名单时实测（取证代理复核后更正了当天先做的"按设计豁免"判断）：

- `POST /api/consents/{id}/revoke` 不收调用方：乙院经办按编号就撤回了甲院患者的同意（200）。
  模块 docstring 的"窗口业务不做可见性阻断"只说登记 / 代提；界面上要先按患者查到同意记录
  （过可见性）才撤得了，撤回不在"本机构还没有他的记录"的场景里。
- `POST /api/consents/corrections/{id}/review` 只收 director（全域）——可自定义角色能被管理员
  「以内置角色为起点」整包授权，这类账号不是全域角色、看不到这个患者，却能批准更正、
  改掉他的主索引姓名（实测 200）。

补上：两处都按记录所属患者判可见性并留痕，先判归属再判状态。全域角色审核照旧，只多一条留痕。
"""
import itertools
import json

import pytest

from app.database import SessionLocal
from app.models import AccessLog, ConsentRecord, CorrectionRequest, Patient


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def consent_world(client):
    """甲院接诊过的患者；乙院经办与之无关；另有一名主任，和乙院一个"主任副本"自定义角色账号。"""
    admin = _login(client, "admin", "admin123")
    a = client.post("/api/organizations",
                    json={"name": "同意归属甲院", "org_type": "lead_hospital", "level": "county"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "同意归属乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    role = client.post("/api/rbac/roles", json={"key": "p028_director_clone", "name": "P0-28 主任副本"},
                       headers=admin)
    assert role.status_code == 201, role.text
    granted = client.post(f"/api/rbac/roles/{role.json()['id']}/permissions",
                          json={"copy_from_builtin": "director"}, headers=admin)
    assert granted.status_code in (200, 201), granted.text
    for uname, rolekey, org in (("p028_doc_a", "doctor", a), ("p028_op_b", "operator", b),
                                ("p028_director", "director", a), ("p028_clone_b", "p028_director_clone", b)):
        r = client.post("/api/users",
                        json={"username": uname, "password": "pw123456", "full_name": uname,
                              "role": rolekey, "org_id": org["id"]},
                        headers=admin)
        assert r.status_code == 201, r.text
    doc_a = _login(client, "p028_doc_a")
    patient = client.post("/api/patients",
                          json={"name": "同意归属患者", "id_card": "320000199101016676"},
                          headers=admin).json()
    enc = client.post("/api/encounters",
                      json={"patient_id": patient["id"], "org_id": a["id"], "encounter_type": "outpatient"},
                      headers=doc_a)
    assert enc.status_code == 201, enc.text
    seq = itertools.count(1)

    def new_consent(revoked=False) -> int:
        from app.models import utcnow

        db = SessionLocal()
        try:
            row = ConsentRecord(patient_id=patient["id"], scene="data_sharing", method="proxy",
                                evidence=f"签字页{next(seq)}", revoked_at=utcnow() if revoked else None)
            db.add(row)
            db.commit()
            return row.id
        finally:
            db.close()

    def new_correction(name: str) -> int:
        db = SessionLocal()
        try:
            row = CorrectionRequest(patient_id=patient["id"], request_type="correction",
                                    changes=json.dumps({"name": name}), reason="户籍更名", source="window")
            db.add(row)
            db.commit()
            return row.id
        finally:
            db.close()

    return {"doc_a": doc_a, "op_b": _login(client, "p028_op_b"), "director": _login(client, "p028_director"),
            "clone_b": _login(client, "p028_clone_b"), "patient_id": patient["id"],
            "new_consent": new_consent, "new_correction": new_correction}


def _logs(patient_id: int, resource: str) -> int:
    db = SessionLocal()
    try:
        return db.query(AccessLog).filter(
            AccessLog.patient_id == patient_id, AccessLog.resource == resource
        ).count()
    finally:
        db.close()


def _revoked(consent_id: int) -> bool:
    db = SessionLocal()
    try:
        return db.get(ConsentRecord, consent_id).revoked_at is not None
    finally:
        db.close()


def _correction_and_name(request_id: int, patient_id: int) -> tuple[str, str]:
    db = SessionLocal()
    try:
        return db.get(CorrectionRequest, request_id).status, db.get(Patient, patient_id).name
    finally:
        db.close()


def test_无关机构不能撤回别家患者的同意(client, consent_world):
    cid = consent_world["new_consent"]()
    r = client.post(f"/api/consents/{cid}/revoke", headers=consent_world["op_b"])
    assert r.status_code == 403, r.text
    assert not _revoked(cid)


def test_有关系的照常撤回且留痕(client, consent_world):
    cid = consent_world["new_consent"]()
    before = _logs(consent_world["patient_id"], "consent")
    r = client.post(f"/api/consents/{cid}/revoke", headers=consent_world["doc_a"])
    assert r.status_code == 200, r.text
    assert _revoked(cid)
    assert _logs(consent_world["patient_id"], "consent") == before + 1


def test_撤回先判归属再判状态(client, consent_world):
    cid = consent_world["new_consent"](revoked=True)
    assert client.post(f"/api/consents/{cid}/revoke", headers=consent_world["op_b"]).status_code == 403, (
        "无关机构拿到 409 就等于知道了这条同意已经撤回"
    )
    assert client.post(f"/api/consents/{cid}/revoke", headers=consent_world["doc_a"]).status_code == 409


def test_整包授了审核权的自定义角色看不到患者就改不了主索引(client, consent_world):
    pid = consent_world["patient_id"]
    rid = consent_world["new_correction"]("乙院改的名")
    r = client.post(f"/api/consents/corrections/{rid}/review", json={"approve": True, "comment": ""},
                    headers=consent_world["clone_b"])
    assert r.status_code == 403, r.text
    assert "无权调阅" in r.json()["detail"], "应当是可见性判定拦下的，不是角色判定（角色判定说明前提变了）"
    assert _correction_and_name(rid, pid) == ("pending", "同意归属患者")


def test_主任照常审核且留痕(client, consent_world):
    pid = consent_world["patient_id"]
    rid = consent_world["new_correction"]("同意归属患者")  # 改成同名：不打扰其余用例对姓名的断言
    before = _logs(pid, "correction")
    r = client.post(f"/api/consents/corrections/{rid}/review", json={"approve": True, "comment": "核对户籍"},
                    headers=consent_world["director"])
    assert r.status_code == 200, r.text
    assert _correction_and_name(rid, pid)[0] == "approved"
    assert _logs(pid, "correction") == before + 1


def test_编号不存在照旧404(client, consent_world):
    assert client.post("/api/consents/987654/revoke", headers=consent_world["op_b"]).status_code == 404
    r = client.post("/api/consents/corrections/987654/review", json={"approve": False, "comment": "x"},
                    headers=consent_world["clone_b"])
    assert r.status_code == 404, r.text
