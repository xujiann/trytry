"""居民自查、服务申请、受理、复核这一路不跑病种排除规则：未成年人经这一路进了成人高血压管理（P2-935，第二十六批
「人口学属性与业务对象的适配」扫描 H2-2）。

医护筛查、批量识别、就诊触发都先跑排除规则、排除压过量表高危；P2-591 明文不许「排除规则挡在门外的人（比如未成年）」
经复核改回目标人群。居民端这一路原先不跑：15 岁的居民自查高血压高危 → `can_apply` → 申请 201 → 受理 200，目标池那一行
从「排除」翻成「目标」（原因栏照写「未成年人不纳入成人高血压管理」），随后签约建档；另一位直接由医护「确认」她那条自查
（自查不跑规则、恒按问卷判疑似），同样翻成目标。

修法：`service.exclusion_problem` 四处共用——自查命中排除的记「排除」、不提示申请并说出是哪条；申请 409；受理只能驳回；
复核确认 409（修前落下的自查疑似、档案事实变了的都挡住）。成年人不受影响。
"""
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.spd.models import SpdCandidate, SpdScreening, SpdServiceApply

B = "/api/spd"
STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
HIGH = {"family": "是", "salt": "是", "overweight": "是", "smoke": "否", "drink": "否", "symptom": "是"}
RULE = "未成年人不纳入成人高血压管理"
TEEN = {"name": "P2935 少年甲", "id_card": "330106201103010019", "gender": "男", "birth_date": "2011-03-01",
        "phone": "13900029351"}
TEEN2 = {"name": "P2935 少年丙", "id_card": "330106201204020021", "gender": "女", "birth_date": "2012-04-02",
         "phone": "13900029352"}
ADULT = {"name": "P2935 成年乙", "id_card": "330106197008150030", "gender": "男", "birth_date": "1970-08-15",
         "phone": "13900029353"}


_TOKENS: dict[str, dict] = {}


def _resident(client, phone):
    """居民令牌按手机号缓存：同一号码一分钟内再要验证码会被限频。"""
    if phone not in _TOKENS:
        code = client.post("/api/portal/auth/sms/code", json={"phone": phone}).json()["debug_code"]
        resp = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code})
        assert resp.status_code == 200, resp.text
        _TOKENS[phone] = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    return _TOKENS[phone]


@pytest.fixture(scope="module")
def world(client, admin):
    _TOKENS.clear()
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2935 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    ids = {}
    for key, person in (("teen", TEEN), ("teen2", TEEN2), ("adult", ADULT)):
        made = client.post("/api/patients", headers=admin, json=person)
        assert made.status_code in (200, 201), made.text
        ids[key] = made.json()["id"]
    for key in ("teen", "teen2"):   # 医护筛查：排除规则先跑，池里那一行是 excluded
        client.post("/api/encounters", headers=admin, json={
            "patient_id": ids[key], "org_id": org, "diagnosis_code": "I10", "diagnosis_name": "原发性高血压"})
        staff = client.post(f"{B}/screenings", headers=admin, json={
            "patient_id": ids[key], "program_code": "hypertension", "org_id": org})
        assert staff.status_code == 201 and staff.json()["result"] == "excluded", staff.text
    return {"org": org, **ids}


def _pool(patient_id):
    with SessionLocal() as db:
        row = db.query(SpdCandidate).filter_by(patient_id=patient_id, program_code="hypertension").first()
        return row.status if row else None


def test_未成年人自查_记排除_不提示申请_说出是哪条(client, world):
    resp = client.post("/api/portal/spd/screenings", headers=_resident(client, TEEN["phone"]), json={
        "program_code": "hypertension", "scale_code": "scr_hypertension", "answers": HIGH})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["result"], body["can_apply"]) == ("excluded", False)   # 修前 suspect / True
    assert RULE in body["excluded_reason"]
    assert body["risk_level"] == "high"   # 量表结论照实给


def test_未成年人申请服务_409(client, world):
    resp = client.post("/api/portal/spd/service-applies", headers=_resident(client, TEEN["phone"]),
                       json={"program_code": "hypertension"})
    assert resp.status_code == 409, resp.text   # 修前 201
    assert "按病种规则不纳入" in resp.json()["detail"] and RULE in resp.json()["detail"]


def test_修前递交的申请_受理只能驳回_池行不翻(client, admin, world):
    with SessionLocal() as db:
        apply = SpdServiceApply(patient_id=world["teen"], program_code="hypertension", status="pending")
        db.add(apply)
        db.commit()
        apply_id = apply.id
    accepted = client.post(f"{B}/service-applies/{apply_id}/handle", headers=admin, json={"status": "accepted"})
    assert accepted.status_code == 409 and "只能驳回" in accepted.json()["detail"], accepted.text   # 修前 200
    assert _pool(world["teen"]) == "excluded"   # 修前翻成 target
    rejected = client.post(f"{B}/service-applies/{apply_id}/handle", headers=admin, json={"status": "rejected"})
    assert rejected.status_code == 200, rejected.text


def test_修前落下的自查疑似_复核确认409(client, admin, world):
    with SessionLocal() as db:   # 修前的自查不跑规则，恒按问卷判疑似
        legacy = SpdScreening(patient_id=world["teen2"], program_code="hypertension", source="self",
                              scale_code="scr_hypertension", answers=HIGH, score=8, risk_level="high",
                              result="suspect")
        db.add(legacy)
        db.commit()
        legacy_id = legacy.id
    confirm = client.post(f"{B}/screenings/{legacy_id}/review", headers=admin, json={"review_result": "confirmed"})
    assert confirm.status_code == 409, confirm.text   # 修前 200，池行翻成 target
    assert RULE in confirm.json()["detail"] and "不能确认为目标人群" in confirm.json()["detail"]
    assert _pool(world["teen2"]) == "excluded"
    exclude = client.post(f"{B}/screenings/{legacy_id}/review", headers=admin, json={"review_result": "excluded"})
    assert exclude.status_code == 200, exclude.text   # 排除照常


def test_成年人照旧_自查可申请_受理入池(client, admin, world):
    ph = _resident(client, ADULT["phone"])
    screened = client.post("/api/portal/spd/screenings", headers=ph, json={
        "program_code": "hypertension", "scale_code": "scr_hypertension", "answers": HIGH})
    assert screened.status_code == 201, screened.text
    assert (screened.json()["result"], screened.json()["can_apply"]) == ("suspect", True)
    assert "excluded_reason" not in screened.json()   # 其余形状字节不变
    applied = client.post("/api/portal/spd/service-applies", headers=ph, json={
        "program_code": "hypertension", "screening_id": screened.json()["id"]})
    assert applied.status_code == 201, applied.text
    handled = client.post(f"{B}/service-applies/{applied.json()['id']}/handle", headers=admin,
                          json={"status": "accepted"})
    assert handled.status_code == 200, handled.text
    assert _pool(world["adult"]) == "target"


def test_居民端写出命中的排除规则():
    src = (STATIC / "m" / "m.js").read_text(encoding="utf-8")
    at = src.index('$("#spd-screen-submit").addEventListener')
    assert "r.excluded_reason" in src[at:at + 3000]
