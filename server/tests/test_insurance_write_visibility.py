"""医保三处按 id 写的归属校验与留痕（P0-29）。

2026-09-24 逐条判 P1-71 名单时实测：

- `POST /api/insurance/referral-certs/{referral_id}`：任一机构的经办都能给别家的转诊签证明（200）。
  先把与患者毫无关系的第三方挡在外面——转出、转入两方本身就有转诊关系，照常能签；
  "到底该哪一方签"是口径问题，另在待裁定清单里。
- 特病审核、双通道审核：只收 director（全域），与 P0-28 的更正审核同一个洞——整包复制
  director 权限的自定义角色不是全域角色、看不到患者，却批得了他的申报。

判定对 director / admin 只多一条留痕。
"""
import itertools

import pytest

from app.database import SessionLocal
from app.models import AccessLog, DualChannelApp, Referral, ReferralCert, SpecialDiseaseApp


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def ins_world(client):
    """甲院把患者转给丙院；乙院经办与之无关；另有主任，与乙院一个"主任副本"自定义角色账号。"""
    admin = _login(client, "admin", "admin123")
    orgs = {}
    for key, name in (("a", "医保归属甲院"), ("b", "医保归属乙院"), ("c", "医保归属丙院")):
        orgs[key] = client.post("/api/organizations",
                                json={"name": name, "org_type": "township", "level": "township"},
                                headers=admin).json()["id"]
    role = client.post("/api/rbac/roles", json={"key": "p029_director_clone", "name": "P0-29 主任副本"},
                       headers=admin)
    assert role.status_code == 201, role.text
    granted = client.post(f"/api/rbac/roles/{role.json()['id']}/permissions",
                          json={"copy_from_builtin": "director"}, headers=admin)
    assert granted.status_code in (200, 201), granted.text
    for uname, rolekey, org in (("p029_op_a", "operator", "a"), ("p029_op_b", "operator", "b"),
                                ("p029_op_c", "operator", "c"), ("p029_director", "director", "a"),
                                ("p029_clone_b", "p029_director_clone", "b")):
        r = client.post("/api/users",
                        json={"username": uname, "password": "pw123456", "full_name": uname,
                              "role": rolekey, "org_id": orgs[org]},
                        headers=admin)
        assert r.status_code == 201, r.text
    patient = client.post("/api/patients",
                          json={"name": "医保归属患者", "id_card": "320000199005056679"},
                          headers=admin).json()

    def add(row) -> int:
        db = SessionLocal()
        try:
            db.add(row)
            db.commit()
            return row.id
        finally:
            db.close()

    pid = patient["id"]
    # 同一患者同一病种 / 药品只能挂一条待审申报（部分唯一索引），每条各用各的名字
    seq = itertools.count(1)
    return {
        "h": {k: _login(client, f"p029_{k}") for k in ("op_a", "op_b", "op_c", "director", "clone_b")},
        "patient_id": pid,
        "new_referral": lambda: add(Referral(patient_id=pid, from_org_id=orgs["a"], to_org_id=orgs["c"],
                                             direction="up", created_by=1, status="accepted")),
        "new_special": lambda: add(SpecialDiseaseApp(patient_id=pid, disease_name=f"特病{next(seq)}", reason="r")),
        "new_dual": lambda: add(DualChannelApp(patient_id=pid, drug_name=f"双通道药{next(seq)}", reason="r", created_by=1)),
    }


def _logs(patient_id: int, resource: str) -> int:
    db = SessionLocal()
    try:
        return db.query(AccessLog).filter(
            AccessLog.patient_id == patient_id, AccessLog.resource == resource
        ).count()
    finally:
        db.close()


def _certs(referral_id: int) -> int:
    db = SessionLocal()
    try:
        return db.query(ReferralCert).filter(ReferralCert.referral_id == referral_id).count()
    finally:
        db.close()


def _status(model, row_id: int) -> str:
    db = SessionLocal()
    try:
        return db.get(model, row_id).status
    finally:
        db.close()


def test_第三方经办不能给别家的转诊签证明(client, ins_world):
    rid = ins_world["new_referral"]()
    r = client.post(f"/api/insurance/referral-certs/{rid}", headers=ins_world["h"]["op_b"])
    assert r.status_code == 403, r.text
    assert _certs(rid) == 0


def test_转出转入两方照常能签且留痕(client, ins_world):
    pid = ins_world["patient_id"]
    for who in ("op_a", "op_c"):
        rid = ins_world["new_referral"]()
        before = _logs(pid, "referral_cert")
        r = client.post(f"/api/insurance/referral-certs/{rid}", headers=ins_world["h"][who])
        assert r.status_code == 200, (who, r.text)
        assert _certs(rid) == 1
        assert _logs(pid, "referral_cert") == before + 1


@pytest.mark.parametrize("kind", ["special", "dual"])
def test_整包复制主任权限的自定义角色看不到患者就批不了申报(client, ins_world, kind):
    if kind == "special":
        app_id, url, model, pending = (ins_world["new_special"](), "special-diseases", SpecialDiseaseApp, "applied")
    else:
        app_id, url, model, pending = (ins_world["new_dual"](), "dual-channel", DualChannelApp, "pending")
    r = client.post(f"/api/insurance/{url}/{app_id}/review?approve=true", headers=ins_world["h"]["clone_b"])
    assert r.status_code == 403, r.text
    assert "无权调阅" in r.json()["detail"], "应当是可见性判定拦下的，不是角色判定（角色判定说明前提变了）"
    assert _status(model, app_id) == pending


@pytest.mark.parametrize("kind", ["special", "dual"])
def test_主任照常审核且留痕(client, ins_world, kind):
    pid = ins_world["patient_id"]
    if kind == "special":
        app_id, url, model, resource = (ins_world["new_special"](), "special-diseases", SpecialDiseaseApp,
                                        "special_disease")
    else:
        app_id, url, model, resource = ins_world["new_dual"](), "dual-channel", DualChannelApp, "dual_channel"
    before = _logs(pid, resource)
    r = client.post(f"/api/insurance/{url}/{app_id}/review?approve=true", headers=ins_world["h"]["director"])
    assert r.status_code == 200, r.text
    assert _status(model, app_id) == "approved"
    assert _logs(pid, resource) == before + 1


def test_编号不存在照旧404(client, ins_world):
    h = ins_world["h"]["clone_b"]
    assert client.post("/api/insurance/referral-certs/987654", headers=ins_world["h"]["op_b"]).status_code == 404
    assert client.post("/api/insurance/special-diseases/987654/review?approve=true", headers=h).status_code == 404
    assert client.post("/api/insurance/dual-channel/987654/review?approve=true", headers=h).status_code == 404
