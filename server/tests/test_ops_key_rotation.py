"""密钥派生与轮换（A1，生产整改包 S）。

守的行为：
1. 签发/写入一律用按用途派生的子密钥（medplat:jwt / medplat:audit / medplat:qr），
   三个用途互相隔离，也不再直接暴露原始 secret 的 MAC；
2. 存量兼容：升级前用**原始 secret 直签**的令牌/审计链/动态码，在新代码下仍验得过；
3. 轮换语义：secret 换新 + MEDPLAT_SECRET_PREVIOUS 设旧后，旧令牌宽限期内可验签、
   旧审计段可验链、动态码换轮瞬间不失效；新签发全部用新密钥的派生口径；
4. 非空洞：文件末尾三条用例把多口径回退掐掉（verification_keys 只留当前派生口径），
   断言存量口径立刻验不过——回退逻辑被删时这里必红。
"""
import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

from app import audit_chain, security
from app.config import settings
from app.gmcrypto import mac
from app.routers import credentials as credentials_module
from app.routers.credentials import _sign, _signature_valid

OLD_SECRET = settings.secret  # 测试环境的当前密钥，轮换用例里把它当"旧密钥"
NEW_SECRET = "b7e29f3c" * 4 + "d4a1"  # 轮换后的新密钥（强度合规，避免误触发凭据守卫）


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _raw_signed_token(secret: str, username: str = "legacy_user") -> str:
    """按升级前的旧代码口径直签一枚令牌：MAC 密钥就是原始 secret 本身。"""
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = {
        "sub": username,
        "role": "admin",
        "iat": time.time(),
        "exp": int(time.time()) + 3600,
        "jti": "legacy001",
    }
    payload = _b64url(json.dumps(claims).encode())
    signature = _b64url(mac(secret.encode(), f"{header}.{payload}".encode()))
    return f"{header}.{payload}.{signature}"


def _build_chain(rows, start_id: int = 1, prev: str = ""):
    """按给定密钥逐条构链。rows 为 (key, path) 序列，允许中途换密钥——
    模拟"前半段旧口径、后半段新口径"的真实存量形态。"""
    entries = []
    for i, (key, path) in enumerate(rows, start=start_id):
        payload = f"{prev}|admin|POST|{path}|200"
        entry_hash = mac(key, payload.encode()).hex()
        entries.append(
            SimpleNamespace(
                id=i, prev_hash=prev, username="admin", method="POST",
                path=path, status_code=200, entry_hash=entry_hash,
            )
        )
        prev = entry_hash
    return entries


def _rotate(monkeypatch):
    """轮换到新密钥：secret 换新、previous 设旧。"""
    monkeypatch.setattr(settings, "secret", NEW_SECRET)
    monkeypatch.setattr(settings, "secret_previous", OLD_SECRET)


# ================================================================ 派生口径


def test_三用途派生密钥互不相同且不等于原始密钥():
    keys = {p: security.derive_key(OLD_SECRET, p) for p in ("jwt", "audit", "qr")}
    assert len(set(keys.values())) == 3, "不同用途必须派生出不同子密钥"
    assert OLD_SECRET.encode() not in keys.values()


def test_新签发令牌用jwt派生密钥而非原始密钥():
    token = security.create_token("alice", "admin")
    header, payload, signature = token.split(".")
    message = f"{header}.{payload}".encode()
    derived = _b64url(mac(security.derive_key(OLD_SECRET, "jwt"), message))
    raw = _b64url(mac(OLD_SECRET.encode(), message))
    assert signature == derived, "签发必须走派生口径"
    assert signature != raw, "签发不得再用原始 secret 直签"
    assert security.decode_token(token) is not None


def test_审计写入用audit派生密钥():
    h = audit_chain.audit_entry_hash("", "admin", "POST", "/api/x", 200)
    payload = b"|admin|POST|/api/x|200"
    assert h == mac(security.derive_key(OLD_SECRET, "audit"), payload).hex()
    assert h != mac(OLD_SECRET.encode(), payload).hex()


def test_动态码签发用qr派生密钥():
    signature = _sign("EHC001.1234567890")
    expected = hmac.new(
        security.derive_key(OLD_SECRET, "qr"), b"EHC001.1234567890", hashlib.sha256
    ).hexdigest()[:32]
    legacy = hmac.new(
        OLD_SECRET.encode(), b"EHC001.1234567890", hashlib.sha256
    ).hexdigest()[:32]
    assert signature == expected
    assert signature != legacy


# ================================================================ 存量兼容（升级不换密钥）


def test_旧原始口径直签的令牌仍可验():
    assert security.decode_token(_raw_signed_token(OLD_SECRET)) is not None


def test_旧原始口径的审计链仍判有效且篡改仍能发现():
    raw_key = OLD_SECRET.encode()
    entries = _build_chain([(raw_key, "/api/a"), (raw_key, "/api/b"), (raw_key, "/api/c")])
    assert audit_chain.verify_chain(entries)["valid"] is True

    entries[1].path = "/api/被篡改"
    verdict = audit_chain.verify_chain(entries)
    assert verdict["valid"] is False and verdict["broken_at"] == entries[1].id


def test_新旧口径混合的审计链整段有效():
    """升级当日的真实形态：前半段原始口径、后半段派生口径，逐条按能通过的口径判真。"""
    raw_key = OLD_SECRET.encode()
    derived = security.derive_key(OLD_SECRET, "audit")
    entries = _build_chain([(raw_key, "/api/old1"), (raw_key, "/api/old2"),
                            (derived, "/api/new1"), (derived, "/api/new2")])
    assert audit_chain.verify_chain(entries)["valid"] is True


def test_旧原始口径的动态码签名仍可核验():
    payload = "EHC001.1234567890"
    legacy_sig = hmac.new(OLD_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    assert _signature_valid(payload, legacy_sig)


# ================================================================ 轮换（secret 换新 + previous 设旧）


def test_轮换后旧令牌两种口径宽限期内均可验(monkeypatch):
    raw_token = _raw_signed_token(OLD_SECRET)
    derived_token = security.create_token("bob", "doctor")  # 旧密钥的派生口径
    _rotate(monkeypatch)
    assert security.decode_token(raw_token) is not None, "previous 原始口径应可验"
    assert security.decode_token(derived_token) is not None, "previous 派生口径应可验"


def test_轮换后新签发全部用新密钥派生口径(monkeypatch):
    _rotate(monkeypatch)
    token = security.create_token("carol", "operator")
    header, payload, signature = token.split(".")
    message = f"{header}.{payload}".encode()
    assert signature == _b64url(mac(security.derive_key(NEW_SECRET, "jwt"), message))
    assert security.decode_token(token) is not None


def test_轮换后未设previous时旧令牌立即失效(monkeypatch):
    """反向语义钉住：不设宽限期就该立刻作废，回退不能宽到"永远有效"。"""
    old_token = security.create_token("dave", "admin")
    monkeypatch.setattr(settings, "secret", NEW_SECRET)
    monkeypatch.setattr(settings, "secret_previous", "")
    assert security.decode_token(old_token) is None


def test_轮换后旧审计段仍可验链(monkeypatch):
    raw_key = OLD_SECRET.encode()
    old_derived = security.derive_key(OLD_SECRET, "audit")
    entries = _build_chain([(raw_key, "/api/old"), (old_derived, "/api/mid")])
    _rotate(monkeypatch)
    # 轮换后新写入的条目（新密钥派生口径）接在旧链之后，整链仍应连续有效
    prev = entries[-1].entry_hash
    entries.append(
        SimpleNamespace(
            id=3, prev_hash=prev, username="admin", method="POST",
            path="/api/new", status_code=200,
            entry_hash=audit_chain.audit_entry_hash(prev, "admin", "POST", "/api/new", 200),
        )
    )
    assert audit_chain.verify_chain(entries)["valid"] is True


def test_轮换换轮瞬间动态码不失效(monkeypatch):
    payload = "EHC002.9999999999"
    raw_sig = hmac.new(OLD_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    derived_sig = _sign(payload)  # 旧密钥派生口径
    _rotate(monkeypatch)
    assert _signature_valid(payload, raw_sig)
    assert _signature_valid(payload, derived_sig)
    # 新出的码用新密钥，也立即可核验
    assert _signature_valid(payload, _sign(payload))


# ================================================================ 非空洞：掐掉回退必须红


def _only_current_derived(purpose: str) -> list[bytes]:
    """模拟"多口径回退被删掉"的实现：只剩当前派生口径。"""
    return [security.signing_key(purpose)]


def test_非空洞_去掉回退后旧口径令牌必验不过(monkeypatch):
    token = _raw_signed_token(OLD_SECRET)
    assert security.decode_token(token) is not None  # 回退在场：绿
    monkeypatch.setattr(security, "verification_keys", _only_current_derived)
    assert security.decode_token(token) is None, "回退被删时本断言应当暴露：旧令牌全体登出"


def test_非空洞_去掉回退后旧口径审计链必判篡改(monkeypatch):
    entries = _build_chain([(OLD_SECRET.encode(), "/api/a"), (OLD_SECRET.encode(), "/api/b")])
    assert audit_chain.verify_chain(entries)["valid"] is True
    monkeypatch.setattr(audit_chain, "verification_keys", _only_current_derived)
    verdict = audit_chain.verify_chain(entries)
    assert verdict["valid"] is False and verdict["broken_at"] == entries[0].id


def test_非空洞_去掉回退后旧口径动态码必核验失败(monkeypatch):
    payload = "EHC003.1111111111"
    legacy_sig = hmac.new(OLD_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    assert _signature_valid(payload, legacy_sig)
    monkeypatch.setattr(credentials_module, "verification_keys", _only_current_derived)
    assert not _signature_valid(payload, legacy_sig)
