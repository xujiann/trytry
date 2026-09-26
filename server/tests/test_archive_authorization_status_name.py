"""档案调阅授权清单：过了有效期的授权标「已过期」、不再摆「撤销」（P2-214）。

`status` 只记「撤没撤」，过了有效期的仍是 active；页面原先只看 status——过期的授权照印「有效」、还摆着「撤销」，
而调阅判定（`visibility.active_authorization_grants`）早已不认它。现在清单带 `effective` 与 `status_name`，
「算不算数」取自同一份有效期口径。
"""
import os

import pytest

from app.database import SessionLocal
from app.models import ArchiveAuthorization, User

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


@pytest.fixture(scope="module")
def world(client, admin):
    grantee = client.post("/api/organizations", headers=admin, json={
        "name": "P2214 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2214 患者", "id_card": "330106197404041669"}).json()["id"]
    ids = {}
    for key, expire in (("expired", "2020-01-01"), ("live", "2099-12-31"), ("revoked", "2099-12-31")):
        resp = client.post(f"/api/patients/{patient}/authorizations", headers=admin,
                           json={"grantee_org_id": grantee, "scope": "all", "expire_date": expire})
        assert resp.status_code == 201, resp.text
        ids[key] = resp.json()["id"]
    # 不设到期日（空串）的只存在于存量数据里（接口现要求填到期日）：直接落库，钉住「空 = 长期有效」与调阅判定一致
    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        row = ArchiveAuthorization(patient_id=patient, grantee_org_id=grantee, scope="all", expire_date="",
                                   created_by=admin_id)
        db.add(row)
        db.commit()
        ids["open"] = row.id
    assert client.post(f"/api/patients/{patient}/authorizations/{ids['revoked']}/revoke", headers=admin).status_code == 200
    return {"patient": patient, "ids": ids}


def test_清单按此刻算不算数给状态(client, admin, world):
    rows = {r["id"]: r for r in client.get(f"/api/patients/{world['patient']}/authorizations", headers=admin).json()}
    got = {key: (rows[i]["effective"], rows[i]["status_name"]) for key, i in world["ids"].items()}
    assert got == {"expired": (False, "已过期"), "live": (True, "有效"), "open": (True, "有效"),
                   "revoked": (False, "已撤销")}                             # 修前过期的也是 active、页面印「有效」


def test_页面按后端给的状态渲染_过期的不摆撤销():
    with open(os.path.join(STATIC, "core.js"), encoding="utf-8") as fh:
        source = fh.read()
    body = source[source.index("const drawAuths = async (pid) => {"):]
    body = body[:body.index("};")]
    assert "esc(a.status_name)" in body and 'a.status === "active" ? "有效"' not in body
    assert "a.effective ? `<button" in body
