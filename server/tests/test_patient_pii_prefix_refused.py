"""证件号 / 电话以密文前缀 `pii1$` 开头的值一律拒收、不落库（P2-1723，第五十一批扫描 AO2-1）。

`EncryptedPII` 见 `pii1$` 前缀就当密文：写入直通（留给回填脚本经 SQL 写），读出一律解密、解不开就抛。修前业务入口
谁都不拦这种值，加密开关开、关两态一样：

- 网页建档电话填 `pii1$13800000000`：回 500，但这一行已经提交；之后默认患者清单、按姓名检索、按卡号取档、360 视图全部 500；
- 档案更正把电话改成 `pii1$abc`（窗口代提与居民端提交走同一个校验函数）：提交 201、审批 200，此后详情、360 都是 500；
- 别家经办推一条 A08（PID-13 写 `pii1$x`）：回执 422「消息解析失败」，覆盖却已经提交；再推正常电话的 A08 同样 422；
- HL7 建档、FHIR Patient 入站同样落得进去。

任何接口都改不回来（取档那一步就抛）。修后：建档请求模型 422（「不得以 pii1$ 开头」）；更正提交 422，修复前已提交的
待审申请审批时 409、档案不动；HL7 / FHIR 入站在解析那一步 422（早于任何写库，ESB 编排共用同一解析）；ORM 层对加密列
（按列类型 `EncryptedPII` 从模型元数据推导）赋值兜底抛错。加解密本身不动；读出侧遇坏密文要不要降级另议（P2-1749）。
"""
import itertools
import json

import pytest
from sqlalchemy import text

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import CorrectionRequest, Patient, SmsCode
from app.pii import EncryptedPII

ID_CARD_SYSTEM = "urn:oid:2.16.156.10011.1.3"
_seq = itertools.count(1)


@pytest.fixture(params=[False, True], ids=["加密关", "加密开"])
def enc(request, monkeypatch):
    monkeypatch.setattr(settings, "pii_encryption_enabled", request.param)
    return request.param


def _card() -> str:
    return f"330782199001{next(_seq):06d}"   # 18 位、用例内唯一（建档只查长度，不核校验位）


def _phone() -> str:
    return f"1380172{next(_seq):04d}"


def _ids(name: str) -> list[int]:
    with engine.connect() as conn:
        return [row.id for row in conn.execute(text("SELECT id FROM patients WHERE name = :n"), {"n": name})]


def _register(client, admin, name: str, phone: str, id_card: str | None = None) -> dict:
    made = client.post("/api/patients", headers=admin, json={"name": name, "id_card": id_card or _card(), "phone": phone})
    assert made.status_code == 201, made.text
    return made.json()


def _phone_of(client, admin, ehc_no: str) -> str:
    got = client.get(f"/api/patients/{ehc_no}", headers=admin)   # 修前：库里是前缀值，取档即抛
    assert got.status_code == 200, got.text
    return got.json()["phone"]   # admin 看明文


def _hl7(event: str, id_card: str, phone: str, name: str, control: str) -> str:
    return (f"MSH|^~\\&|HIS|X|MEDPLAT|COUNTY|20261009091000||ADT^{event}|{control}|P|2.4\r"
            f"PID|1||{id_card}^^^CN^ID||{name}||19900101|M|||||{phone}")


def _portal_login(client, phone: str) -> dict:
    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()
    code = client.post("/api/portal/auth/sms/code", json={"phone": phone, "purpose": "login"}).json()["debug_code"]
    body = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code}).json()
    return {"Authorization": f"Bearer {body['access_token']}"}


def test_建档_证件号或电话以前缀开头_422且不落库(client, admin, enc):
    name = f"P2-1723 建档{int(enc)}"
    for body in ({"id_card": _card(), "phone": "pii1$13800000000"}, {"id_card": "pii1$1234567890", "phone": ""}):
        resp = client.post("/api/patients", headers=admin, json={"name": name, **body})
        assert resp.status_code == 422, resp.text   # 修前 500，这一行已经提交
        assert "不得以 pii1$ 开头" in resp.json()["detail"][0]["msg"], resp.json()
    assert _ids(name) == []
    assert client.get("/api/patients", headers=admin).status_code == 200
    # 正常号码照旧
    phone = _phone()
    made = _register(client, admin, name, phone)
    assert made["created"] is True and _phone_of(client, admin, made["ehc_no"]) == phone


def test_档案更正_电话改成前缀值_窗口与居民端提交即422(client, admin, enc):
    phone = _phone()
    patient = _register(client, admin, f"P2-1723 更正{int(enc)}", phone)
    window = client.post("/api/consents/corrections", headers=admin, json={
        "patient_id": patient["id"], "request_type": "correction", "changes": {"phone": "pii1$abc"}, "reason": "换号"})
    assert window.status_code == 422, window.text   # 修前 201，审批通过后这位患者的详情、360 全部 500
    assert window.json()["detail"].startswith("更正字段 phone：不得以 pii1$ 开头"), window.json()
    resident = _portal_login(client, phone)   # 登录手机号唯一命中这份档案，自动实名绑定
    portal = client.post("/api/portal/me/corrections", headers=resident,
                         json={"changes": {"phone": " pii1$abc "}, "reason": "换号"})
    assert portal.status_code == 422, portal.text   # 去首尾空白后才判（审批落库的就是去空白的值）
    assert portal.json()["detail"].startswith("更正字段 phone：不得以 pii1$ 开头"), portal.json()
    # 正常更正照旧
    new_phone = _phone()
    ok = client.post("/api/portal/me/corrections", headers=resident, json={"changes": {"phone": new_phone}, "reason": "换号"})
    assert ok.status_code == 201, ok.text
    approved = client.post(f"/api/consents/corrections/{ok.json()['id']}/review", headers=admin, json={"approve": True})
    assert approved.status_code == 200, approved.text
    assert _phone_of(client, admin, patient["ehc_no"]) == new_phone


def test_修复前已提交的前缀值更正_审批时409且档案不动(client, admin, enc):
    phone = _phone()
    patient = _register(client, admin, f"P2-1723 待审{int(enc)}", phone)
    with SessionLocal() as db:
        legacy = CorrectionRequest(
            patient_id=patient["id"], request_type="correction", changes=json.dumps({"phone": "pii1$abc"}),
            reason="修复前提交的申请", source="window")
        db.add(legacy)
        db.commit()
        legacy_id = legacy.id
    resp = client.post(f"/api/consents/corrections/{legacy_id}/review", headers=admin, json={"approve": True})
    assert resp.status_code == 409, resp.text   # 修前 200，档案电话成了前缀值
    assert "不得以 pii1$ 开头" in resp.json()["detail"], resp.json()
    assert _phone_of(client, admin, patient["ehc_no"]) == phone
    with SessionLocal() as db:
        assert db.get(CorrectionRequest, legacy_id).status == "pending"


def test_HL7_A08_前缀电话_解析即拒_档案不被覆盖(client, admin, enc):
    card, phone = _card(), _phone()
    name = f"P2-1723 A08{int(enc)}"
    patient = _register(client, admin, name, phone, id_card=card)
    resp = client.post("/api/integration/hl7v2/adt", headers=admin,
                       json={"message": _hl7("A08", card, "pii1$x", name, f"P1723A{int(enc)}")})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"].startswith("PID-13 联系电话不得以 pii1$ 开头"), resp.json()   # 修前「消息解析失败」
    assert _phone_of(client, admin, patient["ehc_no"]) == phone   # 修前覆盖已提交：取档 500
    # 正常电话的 A08 照旧覆盖
    new_phone = _phone()
    fixed = client.post("/api/integration/hl7v2/adt", headers=admin,
                        json={"message": _hl7("A08", card, new_phone, name, f"P1723B{int(enc)}")})
    assert fixed.status_code == 201, fixed.text
    assert _phone_of(client, admin, patient["ehc_no"]) == new_phone


@pytest.mark.parametrize("endpoint", ["/api/integration/hl7v2/patient", "/api/integration/hl7v2/adt"])
def test_HL7_建档_证件号或电话以前缀开头_解析即拒不落库(client, admin, enc, endpoint):
    name = f"P2-1723 HL7{endpoint[-3:]}{int(enc)}"
    for card, phone, label in ((_card(), "pii1$x", "PID-13 联系电话"), ("pii1$1234567890", _phone(), "PID-3 身份证号")):
        resp = client.post(endpoint, headers=admin, json={"message": _hl7("A04", card, phone, name, "P1723C")})
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"].startswith(f"{label}不得以 pii1$ 开头"), resp.json()
    assert _ids(name) == []   # 修前回执 422「消息解析失败」，建档却已经提交
    assert client.get("/api/patients", headers=admin).status_code == 200


def test_FHIR_Patient_证件号或电话以前缀开头_解析即拒不落库(client, admin, enc):
    name = f"P2-1723 FHIR{int(enc)}"
    for card, phone, label in ((_card(), "pii1$x", "telecom 电话"), ("pii1$1234567890", _phone(), "identifier 身份证号")):
        resp = client.post("/api/integration/fhir/Patient", headers=admin, json={
            "resourceType": "Patient", "identifier": [{"system": ID_CARD_SYSTEM, "value": card}],
            "name": [{"text": name}], "telecom": [{"system": "phone", "value": phone}]})
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"].startswith(f"{label}不得以 pii1$ 开头"), resp.json()
    assert _ids(name) == []
    assert client.get("/api/patients", headers=admin).status_code == 200


def test_ESB编排转换_与入站同一解析_前缀值记解析失败():
    from app.routers.esb import _apply_transform

    message = _hl7("A04", _card(), "pii1$x", "P2-1723 ESB", "P1723E")
    with pytest.raises(ValueError, match=r"PID-13 联系电话不得以 pii1\$ 开头"):
        _apply_transform({"message": message}, {"format": "hl7v2_patient"})


#: 加密列清单从模型元数据推导（列类型是 `EncryptedPII` 就算，与 `test_pii_query_point_guard` 同一判据）：以后新加的加密列
#: 自动进分母，没挂兜底的当场变红
ENCRYPTED = sorted(
    ((mapper.class_, key) for mapper in Base.registry.mappers for key, column in mapper.columns.items()
     if isinstance(column.type, EncryptedPII)),
    key=lambda pair: (pair[0].__name__, pair[1]),
)


def test_加密列清单_从元数据推导_含已知三列():
    assert {(cls.__name__, key) for cls, key in ENCRYPTED} >= {
        ("Patient", "id_card"), ("Patient", "phone"), ("ResidentAccount", "phone")}


@pytest.mark.parametrize(("model", "key"), ENCRYPTED, ids=[f"{cls.__name__}.{key}" for cls, key in ENCRYPTED])
def test_ORM兜底_加密列赋前缀值即抛_正常值照收(model, key):
    with pytest.raises(ValueError, match=r"不得以 pii1\$ 开头"):
        model(**{key: "pii1$x"})
    obj = model(**{key: "13800000000"})
    with pytest.raises(ValueError, match=r"不得以 pii1\$ 开头"):
        setattr(obj, key, "pii1$x")
    assert getattr(obj, key) == "13800000000"


def test_ORM兜底_经会话赋值也落不进去(client, admin, enc):
    phone = _phone()
    patient = _register(client, admin, f"P2-1723 ORM{int(enc)}", phone)
    with SessionLocal() as db:
        row = db.get(Patient, patient["id"])
        with pytest.raises(ValueError, match=r"Patient\.phone 不得以 pii1\$ 开头"):
            row.phone = "pii1$x"
        db.commit()   # 脚本照常往下提交：赋值没成，提交的是原值
    assert _phone_of(client, admin, patient["ehc_no"]) == phone
