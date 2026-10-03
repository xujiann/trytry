"""医保转诊证明签发：「转诊单不属于此患者」的 422 排在患者可见性判定之前（P2-1247，第三十六批「接口的 HTTP 语义」扫描 U3-10）。

`insurance.issue_referral_cert` 带了 `patient_id` 先核对转诊单的患者（P2-997），不符就 422，之后才 `assert_patient_visible`：
与这些患者毫无关系的机构经办，拿一张转诊单号逐个试患者号，别人都回 422，回 403 的那一个就是单子的主人——等于知道此人做过
上转，整个过程还不留调阅痕迹。P2-565 定下的规矩是先判可见性、再说状态或归属。

修法：可见性判定挪到这条 422 之前（照 P2-565 的写法）。无关机构对任意患者号都回 403，不再分出 422 / 403；有关机构
（转出、转入两方）撞错患者照旧 422，对上了照常签发。
"""
import pytest

from app.database import SessionLocal
from app.models import ReferralCert


def _login(client, username, password="pw123456"):
    token = client.post("/api/auth/login", json={"username": username, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    """甲镇把患者 2 上转县医院（已接诊）；乙镇与这些患者都没有关系。两家卫生院各一名经办。"""
    orgs = {}
    for key, name, kind, level in (("town", "P21247 甲镇卫生院", "township", "township"),
                                   ("other", "P21247 乙镇卫生院", "township", "township"),
                                   ("county", "P21247 县医院", "lead_hospital", "county")):
        orgs[key] = client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": kind, "level": level}).json()["id"]
    for username, org in (("p21247_op_town", "town"), ("p21247_op_other", "other")):
        made = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pw123456", "full_name": username, "role": "operator",
            "org_id": orgs[org]})
        assert made.status_code == 201, made.text
    patients = []
    for i in range(4):
        made = client.post("/api/patients", headers=admin, json={
            "name": f"P21247 患者{i}", "id_card": f"1101011990011{2470 + i:04d}X"})
        assert made.status_code in (200, 201), made.text
        patients.append(made.json()["id"])
    referral = client.post("/api/referrals", headers=admin, json={
        "patient_id": patients[2], "from_org_id": orgs["town"], "to_org_id": orgs["county"], "direction": "up",
        "reason": "P21247 胸痛待查"}).json()
    assert client.patch(f"/api/referrals/{referral['id']}/status", headers=admin,
                        json={"status": "accepted"}).status_code == 200
    return {
        "referral": referral["id"], "owner": patients[2], "patients": patients,
        "town": _login(client, "p21247_op_town"), "other": _login(client, "p21247_op_other"),
    }


def _issue(client, world, headers, patient_id):
    return client.post(f"/api/insurance/referral-certs/{world['referral']}", headers=headers,
                       params={"patient_id": patient_id})


def _certs(referral_id: int) -> int:
    with SessionLocal() as db:
        return db.query(ReferralCert).filter(ReferralCert.referral_id == referral_id).count()


def test_无关机构对任意患者号都回403_试不出转诊单是谁的(client, world):
    answers = {pid: _issue(client, world, world["other"], pid) for pid in world["patients"]}
    codes = {pid: r.status_code for pid, r in answers.items()}
    # 修前别人都是 422「该转诊单不属于此患者」，只有单子的主人回 403——一眼就试出来了
    assert set(codes.values()) == {403}, codes
    assert all("无权调阅" in r.json()["detail"] for r in answers.values()), {p: r.text for p, r in answers.items()}
    assert _certs(world["referral"]) == 0


def test_有关机构撞错患者照旧422_对上了照常签发(client, world):
    wrong = next(pid for pid in world["patients"] if pid != world["owner"])
    resp = _issue(client, world, world["town"], wrong)
    assert resp.status_code == 422 and resp.json()["detail"] == "该转诊单不属于此患者", resp.text
    assert _certs(world["referral"]) == 0
    right = _issue(client, world, world["town"], world["owner"])
    assert right.status_code == 200 and set(right.json()) == {"cert_no", "referral_id"}, right.text
    assert _certs(world["referral"]) == 1
