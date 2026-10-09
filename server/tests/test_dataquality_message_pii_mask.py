"""数据质控违规说明把 PII 列的原值拼进去：规则落在证件号上时，药师调 `/api/dataquality/run` 读到明文身份证号（P2-1566，
第四十六批扫描 AJ2-1 的脱敏那半）。

修前（scan46 aj2 `r6_dq_pii.py` 实测）：区间 / 枚举 / 引用三类执行器把 `f"{field}={value}"` 原样拼进说明，乙镇药师调
`/run` 读到 `id_card=990101197001011234 超出上限 66`；开着 PII 列加密（库里存的是 `pii1$…` 密文）读出来的也已解密成
明文。同一个账号在患者检索里看到的是 `9901**********1234`。出口脱敏闸门按出参字段名 `id_card` / `phone` 认，`message`
是自由文本，闸门看不见。

修后：说明里遇到 PII 列（列类型是加密列 `EncryptedPII`，或列名是 id_card / phone）时按调用方角色走
`privacy.mask_id_card` / `mask_phone`：admin 照旧明文（逐字节不变），其余角色掩码。会印列取值的两个逻辑校验
（`date_not_future`、`datetime_order`）同一处理。`/summary` 只出计数、不带说明，不用动。授权那一半（谁能调这两个接口、
要不要留痕）按 CLAUDE.md §8 另请人复核，不在本条。
"""
import pytest
from sqlalchemy import text

from app.config import settings
from app.database import SessionLocal
from conftest import business_today_str, login

Q = "/api/dataquality"
ID_CARD, PHONE = "990101197001011234", "13912345678"
MASKED_ID, MASKED_PHONE = "9901**********1234", "139******78"
#: 开着 PII 列加密时另建的一份档案（证件号落密文）
ENC_ID_CARD, ENC_MASKED_ID = "990102198001011238", "9901**********1238"


def _plain_messages(id_card: str, phone: str) -> dict[str, str]:
    """修前的说明原文（admin 修后照旧逐字节是这几句）。"""
    return {
        "P21566_RANGE": f"id_card={id_card} 超出上限 66",
        "P21566_ENUM": f"phone={phone} 不在允许取值 ['无'] 内",
        "P21566_REF": f"id_card={id_card} 不存在于 patients 目录",
        "P21566_FUTURE": f"id_card（{id_card}）晚于当前日期（{business_today_str()}）",
    }


@pytest.fixture(scope="module")
def world(client, admin):
    """乙镇的药师、医生各一；一份带证件号与电话的档案；四条落在证件号 / 电话上的规则（区间、枚举、引用、逻辑各一）。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21566 乙镇卫生院", "org_type": "township", "level": "township"})
    assert org.status_code in (200, 201), org.text
    heads = {}
    for username, role in (("p21566_ph", "pharmacist"), ("p21566_doc", "doctor")):
        made = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": role, "org_id": org.json()["id"]})
        assert made.status_code in (200, 201), made.text
        heads[role] = login(client, username, "passw0rd1")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21566 赵某", "id_card": ID_CARD, "phone": PHONE, "gender": "男", "birth_date": "1970-01-01"})
    assert patient.status_code == 201, patient.text
    rules = {
        "P21566_RANGE": ("range", {"field": "id_card", "min": "11", "max": "66"}),
        "P21566_ENUM": ("enum", {"field": "phone", "values": ["无"]}),
        "P21566_REF": ("cross_ref", {"field": "id_card", "ref_table": "patients", "ref_field": "ehc_no"}),
        "P21566_FUTURE": ("logic", {"check": "date_not_future", "field": "id_card"}),
    }
    for code, (rule_type, config) in rules.items():
        made = client.post(f"{Q}/rules", headers=admin, json={
            "code": code, "name": f"{code} 规则", "target_table": "patients", "rule_type": rule_type, "config": config})
        assert made.status_code == 201, made.text
    return {"heads": heads, "patient": patient.json()["id"]}


def _messages(client, head, record_id: int) -> dict[str, str]:
    """这份档案在四条规则上的违规说明：{规则编码: 说明}。"""
    got = client.get(f"{Q}/run", headers=head, params={"target_table": "patients", "limit": 1000})
    assert got.status_code == 200, got.text
    return {v["rule_code"]: v["message"] for v in got.json()["items"]
            if v["rule_code"].startswith("P21566_") and v["record_id"] == record_id}


def test_admin照旧明文_逐字节不变(client, admin, world):
    assert _messages(client, admin, world["patient"]) == _plain_messages(ID_CARD, PHONE)


@pytest.mark.parametrize("role", ["pharmacist", "doctor"])
def test_药师医生读到的说明是掩码_不出明文(client, world, role):
    got = _messages(client, world["heads"][role], world["patient"])
    expected = {code: text.replace(ID_CARD, MASKED_ID).replace(PHONE, MASKED_PHONE)
                for code, text in _plain_messages(ID_CARD, PHONE).items()}
    assert got == expected   # 修前四句都是明文：id_card=990101197001011234 超出上限 66 ……
    assert not any(ID_CARD in text or PHONE in text for text in got.values())


def test_开着PII列加密_非admin照样掩码_admin照旧明文(client, admin, world, monkeypatch):
    """开态下库里是 `pii1$` 密文，读出来已解密：修前药师看到的就是解密后的明文。"""
    monkeypatch.setattr(settings, "pii_encryption_enabled", True)
    made = client.post("/api/patients", headers=admin, json={
        "name": "P21566 钱某", "id_card": ENC_ID_CARD, "phone": PHONE, "gender": "女", "birth_date": "1980-01-01"})
    assert made.status_code == 201, made.text
    with SessionLocal() as db:   # 落库确实是密文（裸 SQL 不过列类型的解密）——说明里的明文只可能是读出来解密的
        raw = db.execute(text("SELECT id_card FROM patients WHERE id = :id"), {"id": made.json()["id"]}).scalar()
        assert raw.startswith("pii1$"), raw[:16]
    pharmacist = _messages(client, world["heads"]["pharmacist"], made.json()["id"])
    assert pharmacist["P21566_RANGE"] == f"id_card={ENC_MASKED_ID} 超出上限 66"   # 修前：id_card=990102198001011238 …
    assert not any(ENC_ID_CARD in text or PHONE in text for text in pharmacist.values()), pharmacist
    assert _messages(client, admin, made.json()["id"]) == _plain_messages(ENC_ID_CARD, PHONE)


def test_汇总只出计数_不带说明(client, world):
    """`/summary` 的出参只有逐规则计数，没有说明文字，这一条不用动；钉住它不出取值。"""
    got = client.get(f"{Q}/summary", headers=world["heads"]["pharmacist"])
    assert got.status_code == 200, got.text
    by_rule = {r["rule_code"]: r["violations"] for r in got.json()["by_rule"]}
    assert by_rule["P21566_RANGE"] >= 1
    assert ID_CARD not in got.text and PHONE not in got.text and "message" not in got.text
