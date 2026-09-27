"""转诊证明记签发人（P2-524，P0-29 残留）。

P0-29 的实测记着：转诊证明任一机构的经办都能签，**签证明的人不落库**。当时修了前一半（先判患者可见性并留痕），
`referral_certs` 仍只有转诊号、证明号与签发时间——证明是凭证，出了争议要问「谁签的」，库里答不上来。

修后签发时记下签发人；复签是幂等的（返回首签那张、不换号），签发人随之不变，不会被后来复签的人改写。
"""
import pytest

from app.database import SessionLocal
from app.models import Referral, ReferralCert


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    """甲院把患者上转给丙院，两院各一名经办。"""
    orgs = {
        key: client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township", "level": "township"}).json()["id"]
        for key, name in (("a", "P2524 甲院"), ("c", "P2524 丙院"))
    }
    users = {}
    for key in ("a", "c"):
        created = client.post("/api/users", headers=admin, json={
            "username": f"p2524_op_{key}", "password": "pw123456", "full_name": f"P2524 经办{key}",
            "role": "operator", "org_id": orgs[key]})
        assert created.status_code == 201, created.text
        users[key] = created.json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2524 患者", "id_card": "330127198805052524"}).json()["id"]
    with SessionLocal() as db:
        referral = Referral(patient_id=patient, from_org_id=orgs["a"], to_org_id=orgs["c"],
                            direction="up", created_by=1, status="accepted")
        db.add(referral)
        db.commit()
        referral_id = referral.id
    return {"referral_id": referral_id, "users": users,
            "h": {key: _login(client, f"p2524_op_{key}") for key in ("a", "c")}}


def _cert(referral_id: int) -> ReferralCert:
    with SessionLocal() as db:
        cert = db.query(ReferralCert).filter(ReferralCert.referral_id == referral_id).one()
        db.expunge(cert)
        return cert


def test_签发时记下签发人(client, world):
    got = client.post(f"/api/insurance/referral-certs/{world['referral_id']}", headers=world["h"]["a"])
    assert got.status_code == 200, got.text
    cert = _cert(world["referral_id"])
    assert cert.cert_no == got.json()["cert_no"]
    assert cert.issued_by == world["users"]["a"]   # 修前没有这一列：谁签的库里答不上来


def test_另一方复签拿回同一张_签发人不被改写(client, world):
    first = _cert(world["referral_id"])
    again = client.post(f"/api/insurance/referral-certs/{world['referral_id']}", headers=world["h"]["c"])
    assert again.status_code == 200, again.text
    assert again.json()["cert_no"] == first.cert_no
    assert _cert(world["referral_id"]).issued_by == world["users"]["a"]
