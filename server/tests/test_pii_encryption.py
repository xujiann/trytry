"""PII 列加密（工程包 E3）：SM4 国标向量、列加密层、开关两态行为与回填脚本。

关态零行为的另一半证据不在本文件——是既有套件原样跑绿
（test_portal_auth / test_consents / test_esec_account / test_stage15_horizontal 等）。
"""
import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import gmcrypto
from app.config import settings
from app.database import SessionLocal, engine
from app.models import Patient, ResidentAccount, SmsCode
from app.pii import PII_PREFIX, decrypt_pii, encrypt_pii, pii_filter, pii_index
from app.routers.portal import _reset_portal_failures

# ---------------------------------------------------------------- SM4 国标向量

_SM4_KEY = bytes.fromhex("0123456789abcdeffedcba9876543210")


def test_sm4_单次加密国标示例向量():
    """GB/T 32907-2016 示例1：明文=密钥，密文 681edf34…。"""
    assert gmcrypto.sm4_encrypt_block(_SM4_KEY, _SM4_KEY).hex() == (
        "681edf34d206965e86b3e94f536e4246"
    )


@pytest.mark.skipif(
    not os.environ.get("MEDPLAT_RUN_SLOW"),
    reason="国标示例2需 100 万次迭代（纯 Python 约 40s），设 MEDPLAT_RUN_SLOW=1 开启",
)
def test_sm4_百万次迭代国标示例向量():
    x = _SM4_KEY
    for _ in range(1_000_000):
        x = gmcrypto.sm4_encrypt_block(_SM4_KEY, x)
    assert x.hex() == "595298c7c6fd271f0402f804c33d3f66"


def test_sm4_ctr_加解密自反与参数校验():
    nonce = os.urandom(16)
    msg = "330782199001011234-测试".encode()
    ct = gmcrypto.sm4_ctr(_SM4_KEY, nonce, msg)
    assert ct != msg
    assert gmcrypto.sm4_ctr(_SM4_KEY, nonce, ct) == msg
    with pytest.raises(ValueError):
        gmcrypto.sm4_encrypt_block(_SM4_KEY, b"short")
    with pytest.raises(ValueError):
        gmcrypto.sm4_ctr(_SM4_KEY, b"short", msg)
    with pytest.raises(ValueError):
        gmcrypto.sm4_encrypt_block(b"badkey", _SM4_KEY)


# ---------------------------------------------------------------- 列加密层单元

def test_加解密往返与存储格式():
    stored = encrypt_pii("330782199001011234")
    assert stored.startswith(PII_PREFIX)
    assert stored.count("$") == 2  # pii1$body$mac
    assert decrypt_pii(stored) == "330782199001011234"
    # 随机 nonce：同一明文两次加密密文不同
    assert encrypt_pii("330782199001011234") != stored


def test_空串与存量明文直通():
    assert encrypt_pii("") == ""
    assert decrypt_pii("") == ""
    assert decrypt_pii("13800001111") == "13800001111"  # 无前缀=存量明文直返


def test_篡改密文必须拒绝解密():
    stored = encrypt_pii("13907001234")
    prefix, body, mac_hex = stored.split("$")
    # 篡改 MAC
    bad_mac = f"{prefix}${body}${'0' * len(mac_hex)}"
    with pytest.raises(ValueError):
        decrypt_pii(bad_mac)
    # 篡改密文体（换一个 base64 字符）
    flipped = ("A" if body[10] != "A" else "B")
    bad_body = f"{prefix}${body[:10]}{flipped}{body[11:]}${mac_hex}"
    with pytest.raises(ValueError):
        decrypt_pii(bad_body)
    # 格式损坏
    with pytest.raises(ValueError):
        decrypt_pii("pii1$不是base64")


def test_检索索引确定性且与加密密钥分离():
    a, b = pii_index("330782199001011234"), pii_index("330782199001011234")
    assert a == b and len(a) == 64  # 确定性，HMAC 十六进制
    assert pii_index("330782199001011235") != a
    # 分键：索引密钥(pii-idx)算出的值 ≠ 加密密钥(pii)对同一明文的 MAC
    from app.pii import _enc_key
    assert gmcrypto.mac(_enc_key(), b"330782199001011234").hex() != a


def test_密钥轮换_旧钥密文经previous回退可解(monkeypatch):
    old_secret = settings.secret
    stored = encrypt_pii("330782199001011234")
    monkeypatch.setattr(settings, "secret", "rotated-secret-for-pii-test")
    with pytest.raises(ValueError):  # 未配 previous：旧钥密文拒绝
        decrypt_pii(stored)
    monkeypatch.setattr(settings, "secret_previous", old_secret)
    assert decrypt_pii(stored) == "330782199001011234"  # 宽限期回退


# ---------------------------------------------------------------- 应用两态行为


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "pii_encryption_enabled", True)
    yield
    monkeypatch.setattr(settings, "pii_encryption_enabled", False)


@pytest.fixture(autouse=True)
def clean_portal_state():
    _reset_portal_failures()
    yield
    _reset_portal_failures()


def _raw(sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).fetchall()


def _send_code(client, phone: str, purpose: str = "login") -> str:
    with SessionLocal() as db:  # 清冷却，允许连续下发
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()
    resp = client.post("/api/portal/auth/sms/code", json={"phone": phone, "purpose": purpose})
    assert resp.status_code == 200, resp.text
    return resp.json()["debug_code"]


def test_关态_建档落明文且索引列已维护(client, admin):
    resp = client.post(
        "/api/patients",
        json={"name": "关态患者", "id_card": "330782199001010011", "phone": "13800010001"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    rows = _raw(
        "SELECT id_card, phone, id_card_idx, phone_idx FROM patients WHERE id = :i",
        i=resp.json()["id"],
    )
    assert rows[0].id_card == "330782199001010011"  # 关态：明文原样
    assert rows[0].phone == "13800010001"
    # 索引列关态同样维护（开关切换不需要重算新增行）
    assert rows[0].id_card_idx == pii_index("330782199001010011")
    assert rows[0].phone_idx == pii_index("13800010001")


def test_关态_EMPI等值查询命中既有档案(client, admin):
    first = client.post(
        "/api/patients",
        json={"name": "关态幂等", "id_card": "330782199001010022"},
        headers=admin,
    ).json()
    again = client.post(
        "/api/patients",
        json={"name": "关态幂等", "id_card": "330782199001010022"},
        headers=admin,
    ).json()
    assert again["ehc_no"] == first["ehc_no"]


# 开态代表性链路一：建档→库里是密文→索引等值检索命中→响应透明解密
def test_开态_建档存密文且全值检索命中(client, admin, enabled):
    resp = client.post(
        "/api/patients",
        json={"name": "开态患者", "id_card": "330782199002020011", "phone": "13800020001"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["id_card"] == "330782199002020011"  # 响应（admin）透明解密
    row = _raw(
        "SELECT id_card, phone, id_card_idx FROM patients WHERE id = :i", i=resp.json()["id"]
    )[0]
    assert row.id_card.startswith(PII_PREFIX) and row.phone.startswith(PII_PREFIX)
    assert decrypt_pii(row.id_card) == "330782199002020011"
    assert row.id_card_idx == pii_index("330782199002020011")
    # 全值关键字检索命中（模糊降级口径：前缀/中缀不支持，全值走索引）
    hits = client.get(
        "/api/patients", params={"keyword": "330782199002020011"}, headers=admin
    ).json()
    assert [p["id_card"] for p in hits] == ["330782199002020011"]
    # ORM 读取透明解密
    with SessionLocal() as db:
        p = db.get(Patient, resp.json()["id"])
        assert p.id_card == "330782199002020011"


# 开态代表性链路二：EMPI 幂等 + 唯一性由索引列部分唯一索引兜底
def test_开态_EMPI幂等与索引唯一兜底(client, admin, enabled):
    first = client.post(
        "/api/patients",
        json={"name": "开态幂等", "id_card": "330782199002020022"},
        headers=admin,
    ).json()
    again = client.post(
        "/api/patients",
        json={"name": "开态幂等", "id_card": "330782199002020022"},
        headers=admin,
    ).json()
    assert again["ehc_no"] == first["ehc_no"]
    # 绕过应用查重直插同证件号：密文互不相同（原唯一约束不触发），
    # 唯一性由 uq_patient_id_card_idx 部分唯一索引兜底
    with SessionLocal() as db:
        db.add(Patient(ehc_no="EHCPIITEST01", name="直插", id_card="330782199002020022"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


# 开态代表性链路三：验证码登录→实名绑定（证件号等值走索引）
def test_开态_实名绑定全链路(client, admin, enabled):
    client.post(
        "/api/patients",
        json={"name": "开态绑定", "id_card": "330782199002020033"},
        headers=admin,
    )
    phone = "13800020033"
    code = _send_code(client, phone)
    login = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code})
    assert login.status_code == 200, login.text
    token = {"Authorization": f"Bearer {login.json()['access_token']}"}
    bound = client.post(
        "/api/portal/auth/realname",
        json={"name": "开态绑定", "id_card": "330782199002020033"},
        headers=token,
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["bound"] is True
    # 居民账户手机号在库里也是密文
    row = _raw("SELECT phone FROM resident_accounts WHERE phone_idx = :i", i=pii_index(phone))[0]
    assert row.phone.startswith(PII_PREFIX)


# 开态代表性链路四：登录手机号唯一命中患者→自动实名绑定（phone 等值走索引）
def test_开态_手机号唯一命中自动绑定(client, admin, enabled):
    client.post(
        "/api/patients",
        json={"name": "开态自动绑", "id_card": "330782199002020044", "phone": "13800020044"},
        headers=admin,
    )
    code = _send_code(client, "13800020044")
    login = client.post(
        "/api/portal/auth/sms/login", json={"phone": "13800020044", "code": code}
    )
    assert login.status_code == 200, login.text
    assert login.json()["bound"] is True
    assert login.json()["name"] == "开态自动绑"


# 开态代表性链路五：就诊凭据 resolve 按证件号命中 + 补绑手机号重复 409
def test_开态_凭据resolve与手机号重复冲突(client, admin, enabled):
    client.post(
        "/api/patients",
        json={"name": "开态凭据", "id_card": "330782199002020055"},
        headers=admin,
    )
    resolved = client.get(
        "/api/credentials/resolve", params={"identifier": "330782199002020055"}, headers=admin
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["matched_by"] == "id_card"
    # 手机号已被别的账户占用：微信登录账户补绑同号 → 409（等值查重走索引）
    phone = "13800020055"
    code = _send_code(client, phone)
    assert client.post(
        "/api/portal/auth/sms/login", json={"phone": phone, "code": code}
    ).status_code == 200
    mock = client.get("/api/portal/auth/wechat/authorize").json()
    wechat = client.post(
        "/api/portal/auth/wechat/login", json={"code": mock["mock_code"], "state": mock["state"]}
    )
    assert wechat.status_code == 200, wechat.text
    token = {"Authorization": f"Bearer {wechat.json()['access_token']}"}
    dup = client.post(
        "/api/portal/auth/bind-phone", json={"phone": phone, "code": "000000"}, headers=token
    )
    assert dup.status_code == 409


def test_开态_关前明文存量仍可命中_混存兼容(client, admin, enabled, monkeypatch):
    # 先在关态建档（明文入库），再开开关：索引列关态就已维护，等值检索照常命中
    monkeypatch.setattr(settings, "pii_encryption_enabled", False)
    created = client.post(
        "/api/patients",
        json={"name": "混存患者", "id_card": "330782199002020066"},
        headers=admin,
    ).json()
    monkeypatch.setattr(settings, "pii_encryption_enabled", True)
    again = client.post(
        "/api/patients",
        json={"name": "混存患者", "id_card": "330782199002020066"},
        headers=admin,
    ).json()
    assert again["ehc_no"] == created["ehc_no"]  # 幂等命中的是明文存量行
    row = _raw("SELECT id_card FROM patients WHERE id = :i", i=created["id"])[0]
    assert not row.id_card.startswith(PII_PREFIX)  # 存量行仍是明文（改写归回填脚本管）


def test_开态_篡改库中密文读取即拒绝(client, admin, enabled):
    created = client.post(
        "/api/patients",
        json={"name": "被篡改", "id_card": "330782199002020077"},
        headers=admin,
    ).json()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE patients SET id_card = :v WHERE id = :i"),
            {"v": encrypt_pii("330782199002020077")[:-4] + "0000", "i": created["id"]},
        )
    with SessionLocal() as db:
        with pytest.raises(ValueError):
            _ = db.get(Patient, created["id"]).id_card
    with engine.begin() as conn:  # 复原，避免污染后续用例的全表扫描
        conn.execute(
            text("UPDATE patients SET id_card = :v WHERE id = :i"),
            {"v": encrypt_pii("330782199002020077"), "i": created["id"]},
        )


def test_非空洞_开态等值检索确实依赖索引列(client, admin, enabled):
    """把索引列清掉后开态查询必落空——证明 pii_filter 走的是索引列而非明文回退；
    若有人删掉写入侧的索引维护，本用例与上面的链路用例会一起变红。"""
    created = client.post(
        "/api/patients",
        json={"name": "索引承重", "id_card": "330782199002020088"},
        headers=admin,
    ).json()
    with SessionLocal() as db:
        cond = pii_filter(Patient.id_card_idx, Patient.id_card, "330782199002020088")
        assert db.query(Patient).filter(cond).first() is not None
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE patients SET id_card_idx = NULL WHERE id = :i"), {"i": created["id"]}
        )
    with SessionLocal() as db:
        cond = pii_filter(Patient.id_card_idx, Patient.id_card, "330782199002020088")
        assert db.query(Patient).filter(cond).first() is None  # 没有索引列就查不到


# ---------------------------------------------------------------- 回填脚本

def _run_backfill(**kwargs) -> dict[str, tuple[int, int, int]]:
    import importlib

    backfill = importlib.import_module("scripts.pii_encrypt_backfill")
    return {
        f"{t}.{c}": backfill.backfill_column(t, c, i, **kwargs)
        for t, c, i in backfill.TARGETS
    }


def test_回填脚本_dry_run不落库_正式跑加密且幂等(client, admin):
    created = client.post(
        "/api/patients",
        json={"name": "回填患者", "id_card": "330782199003030011", "phone": "13800030001"},
        headers=admin,
    ).json()
    # dry-run：统计但不改库
    _run_backfill(dry_run=True)
    row = _raw("SELECT id_card FROM patients WHERE id = :i", i=created["id"])[0]
    assert not row.id_card.startswith(PII_PREFIX)
    # 正式跑：明文改写为密文，索引重算，ORM 读取透明解密
    _run_backfill()
    row = _raw("SELECT id_card, phone, id_card_idx FROM patients WHERE id = :i", i=created["id"])[0]
    assert row.id_card.startswith(PII_PREFIX) and row.phone.startswith(PII_PREFIX)
    assert decrypt_pii(row.id_card) == "330782199003030011"
    assert row.id_card_idx == pii_index("330782199003030011")
    with SessionLocal() as db:
        assert db.get(Patient, created["id"]).id_card == "330782199003030011"
    # 幂等：再跑一遍全部跳过、零改写
    for name, (rewritten, skipped, failed) in _run_backfill().items():
        assert rewritten == 0 and failed == 0, name


def test_回填脚本_密钥轮换重加密(client, admin, monkeypatch):
    created = client.post(
        "/api/patients",
        json={"name": "轮换患者", "id_card": "330782199003030022"},
        headers=admin,
    ).json()
    _run_backfill()
    old_secret = settings.secret
    monkeypatch.setattr(settings, "secret", "rotated-secret-for-pii-test")
    # 新钥下旧密文：--old-secret 解开重加密，索引按新钥重算
    _run_backfill(old_secret=old_secret)
    row = _raw("SELECT id_card, id_card_idx FROM patients WHERE id = :i", i=created["id"])[0]
    assert decrypt_pii(row.id_card) == "330782199003030022"  # 当前（新）钥可解
    assert row.id_card_idx == pii_index("330782199003030022")  # 新钥索引
    # 重跑（同参）幂等：已是新钥密文，跳过
    for name, (rewritten, skipped, failed) in _run_backfill(old_secret=old_secret).items():
        assert rewritten == 0 and failed == 0, name
    # 收尾：回滚 secret 后把测试库改写回当前 secret 的密文，避免污染后续模块
    monkeypatch.setattr(settings, "secret", old_secret)
    _run_backfill(old_secret="rotated-secret-for-pii-test")


def test_回填脚本_覆盖居民账户手机号(client, admin):
    phone = "13800030033"
    code = _send_code(client, phone)
    assert client.post(
        "/api/portal/auth/sms/login", json={"phone": phone, "code": code}
    ).status_code == 200
    _run_backfill()
    row = _raw(
        "SELECT phone FROM resident_accounts WHERE phone_idx = :i", i=pii_index(phone)
    )[0]
    assert row.phone.startswith(PII_PREFIX)
    assert decrypt_pii(row.phone) == phone
    with SessionLocal() as db:  # ORM 透明解密
        acc = db.query(ResidentAccount).filter(
            pii_filter(ResidentAccount.phone_idx, ResidentAccount.phone, phone)
        ).first()
        assert acc is not None and acc.phone == phone
