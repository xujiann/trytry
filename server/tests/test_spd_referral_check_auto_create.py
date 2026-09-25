"""转诊规则试算勾「自动开单」推不出发起机构即 500（P2-76）。

发起机构取账号绑定的机构；管理员 / 管理层这类不绑机构的账号代录时，回落到患者在所填病种下的纳管档案所在
机构。发起转诊（`POST /referrals`）两者都没有时 422「账号未绑定机构且患者未纳管，无法确定发起机构」。
规则试算（`POST /referral-rules/check`）勾了自动开单直接调同一个建单帮手 `_create_case`，这句判定却只写在
发起转诊里——发起机构为空撞 `initiator_org_id` 的非空约束，500。判定挪进 `_create_case`，两条路径共用一句。
"""
import pytest

B = "/api/spd"
NO_ORIGIN = "账号未绑定机构且患者未纳管，无法确定发起机构"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P276 转诊试算院", "org_type": "township", "level": "township"}).json()["id"]
    r = client.post(f"{B}/programs", headers=admin, json={"code": "P276_PG", "name": "P276 病种", "category": "chronic"})
    assert r.status_code == 201, r.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P276 患者", "id_card": "330106197001011346", "gender": "男", "birth_date": "1970-01-01"}).json()["id"]
    r = client.post(f"{B}/enrollments", headers=admin,
                    json={"patient_id": patient, "program_code": "P276_PG", "org_id": org})
    assert r.status_code == 201, r.text
    # 不限病种的规则，靠请求带的额外事实命中：病种编码留空也走到开单那一步
    r = client.post(f"{B}/referral-rules", headers=admin, json={
        "code": "P276_ANY", "name": "P276 通用规则", "conditions": [{"field": "p276_flag", "op": "==", "value": "yes"}]})
    assert r.status_code == 201, r.text
    return {"org": org, "patient": patient}


def _cases(patient_id):
    from app.database import SessionLocal
    from app.spd.models import SpdReferralCase

    with SessionLocal() as db:
        return db.query(SpdReferralCase).filter(SpdReferralCase.patient_id == patient_id).count()


def _check(client, admin, world, program_code, auto_create):
    return client.post(f"{B}/referral-rules/check", headers=admin, json={
        "patient_id": world["patient"], "program_code": program_code, "extra": {"p276_flag": "yes"},
        "auto_create": auto_create})


def test_推不出发起机构_自动开单与发起转诊同一句422_不留半张单(client, admin, world):
    """管理员不绑机构、病种编码留空（推不出纳管档案）：修前自动开单撞 initiator_org_id 非空约束 500。"""
    before = _cases(world["patient"])
    r = _check(client, admin, world, "", True)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == NO_ORIGIN
    r = client.post(f"{B}/referrals", headers=admin, json={"patient_id": world["patient"], "reason": "P276 手工"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == NO_ORIGIN
    assert _cases(world["patient"]) == before


def test_只试算不开单_推不出发起机构也照常给出命中(client, admin, world):
    r = _check(client, admin, world, "", False)
    assert r.status_code == 200, r.text
    assert r.json()["triggered"] is True
    assert r.json()["case"] is None


def test_患者在该病种下纳管_回落到档案机构照常开单(client, admin, world):
    r = _check(client, admin, world, "P276_PG", True)
    assert r.status_code == 200, r.text
    case = r.json()["case"]
    assert case is not None
    assert case["initiator_org_id"] == world["org"]
