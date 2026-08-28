"""打印件防伪验真（ADR-0015）：签名令牌 + 公开最小披露核验。

守住的东西，按重要程度排：

1. **不可伪造/不可枚举**：改动令牌任何一段（载荷、签名、前缀）都验不过，
   且失败响应不区分"哪里不对"——探测者拿不到任何梯度；
2. **纸比密钥长寿**：密钥轮换进宽限期（secret_previous）后，旧密钥签发的
   打印件必须仍验得过；宽限期结束（previous 清空）才失效——与登录令牌、
   审计链同一条轮换纪律（test_ops_key_rotation 的姊妹篇）；
3. **公开但有闸**：核验端点免登录（核验者在平台外），靠限速挡批量探测；
4. **最小披露**：核验响应只含纸面已印字段，任何患者身份信息都不得出现；
5. **12 种单据全覆盖**：VERIFY_REGISTRY 与 DOC_TYPES 键集合一致——加新单据
   漏配注册表，这里第一时间变红，而不是"印了验真码却永远查无此单"。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.config import settings
from app.main import app
from app.printverify import (
    VERIFY_REGISTRY,
    make_verify_token,
    parse_verify_token,
    verify_document,
)
from app.routers import printing
from app.state_store import SlidingWindowRateLimiter


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:  # 进上下文才跑 lifespan，admin 账号在那里种
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def data(client, admin):
    """一套最小打印素材：机构 + 患者 + 转诊单 + 已撤回的知情同意。"""
    from app.database import SessionLocal
    from app.models import ConsentRecord, Organization, Patient, Referral, User

    db = SessionLocal()
    admin_user = db.query(User).filter_by(username="admin").first()
    org = Organization(name="验真测试医院", org_type="lead_hospital", level="county")
    db.add(org)
    db.commit()
    patient = Patient(
        name="验真测试患者", ehc_no="EHCPRTVRF01", gender="女",
        birth_date="1975-06-01", id_card="110101197506010027",
    )
    db.add(patient)
    db.commit()
    referral = Referral(
        patient_id=patient.id, from_org_id=org.id, to_org_id=org.id,
        direction="up", reason="验真用例", status="pending", created_by=admin_user.id,
    )
    consent = ConsentRecord(patient_id=patient.id, scene="archive", method="proxy")
    db.add_all([referral, consent])
    db.commit()
    ids = {"org": org.id, "patient": patient.id, "referral": referral.id, "consent": consent.id}
    db.close()
    return ids


# ================================================================ 令牌本身


def test_令牌往返_载荷字段原样取回():
    token = make_verify_token(
        doc_type="referral", doc_id=7, doc_no="ZZ00000007",
        org_name="某县人民医院", issued_date="2026-08-28",
    )
    data = parse_verify_token(token)
    assert data == {"t": "referral", "i": 7, "n": "ZZ00000007",
                    "o": "某县人民医院", "d": "2026-08-28"}


def test_篡改任何一段都验不过():
    token = make_verify_token(
        doc_type="referral", doc_id=7, doc_no="ZZ00000007",
        org_name="某县人民医院", issued_date="2026-08-28",
    )
    prefix, payload, mac = token.split(".")
    # 换载荷（把 doc_id 改成 8 重新编码）——签名对不上
    other = make_verify_token(
        doc_type="referral", doc_id=8, doc_no="ZZ00000008",
        org_name="某县人民医院", issued_date="2026-08-28",
    ).split(".")[1]
    assert parse_verify_token(f"{prefix}.{other}.{mac}") is None
    # 改签名
    flipped = mac[:-2] + ("AA" if not mac.endswith("AA") else "BB")
    assert parse_verify_token(f"{prefix}.{payload}.{flipped}") is None
    # 改版本前缀 / 少一段 / 纯垃圾 / 空串
    assert parse_verify_token(f"v2.{payload}.{mac}") is None
    assert parse_verify_token(f"{payload}.{mac}") is None
    assert parse_verify_token("不是令牌") is None
    assert parse_verify_token("") is None


def test_密钥轮换宽限期内旧令牌仍有效_宽限结束失效(monkeypatch):
    """纸会活很多年：轮换纪律与登录令牌/审计链完全一致（security.verification_keys）。"""
    old_secret = settings.secret
    token = make_verify_token(
        doc_type="referral", doc_id=7, doc_no="ZZ00000007",
        org_name="某县人民医院", issued_date="2026-08-28",
    )
    monkeypatch.setattr(settings, "secret", "rotated-new-secret-0123456789")
    monkeypatch.setattr(settings, "secret_previous", old_secret)
    assert parse_verify_token(token) is not None, "宽限期内旧密钥签的打印件必须仍验得过"
    monkeypatch.setattr(settings, "secret_previous", "")
    assert parse_verify_token(token) is None, "宽限期结束后旧令牌应失效"


# ================================================================ 核验语义


def test_注册表与打印单据类型一一对应():
    """加新单据只改 DOC_TYPES 不配 VERIFY_REGISTRY，印出去的码永远"查无此单"。"""
    assert set(VERIFY_REGISTRY) == set(printing.DOC_TYPES)


def test_核验通过_返回纸面字段与现势状态(client, data, admin):
    token = make_verify_token(
        doc_type="referral", doc_id=data["referral"], doc_no=f"ZZ{data['referral']:08d}",
        org_name="验真测试医院", issued_date="2026-08-28",
    )
    body = client.get("/api/print/verify", params={"token": token}).json()
    assert body["valid"] is True
    assert body["doc_label"] == "转诊单"
    assert body["status"] == "有效"
    # 最小披露：响应里不允许出现任何患者身份信息
    assert "验真测试患者" not in str(body)
    assert "110101197506010027" not in str(body)


def test_核验端点免登录(client, data):
    """核验者在平台外没有账号——公开是设计而非疏漏（ADR-0015 约束 1）。"""
    token = make_verify_token(
        doc_type="referral", doc_id=data["referral"], doc_no="X",
        org_name="X", issued_date="2026-08-28",
    )
    resp = client.get("/api/print/verify", params={"token": token})  # 无 Authorization
    assert resp.status_code == 200


def test_签名有效但记录不存在_如实相告而非判假(client):
    token = make_verify_token(
        doc_type="referral", doc_id=99999999, doc_no="ZZ99999999",
        org_name="验真测试医院", issued_date="2020-01-01",
    )
    body = client.get("/api/print/verify", params={"token": token}).json()
    assert body["valid"] is False
    assert body["doc_no"] == "ZZ99999999", "纸面字段要给出来——把真纸显示成假件会冤枉人"
    assert "查不到" in body["reason"]


def test_无效令牌统一失败_不给探测梯度(client):
    body = client.get("/api/print/verify", params={"token": "v1.AAAA.BBBB"}).json()
    assert body["valid"] is False
    assert body["doc_no"] == "" and body["org_name"] == ""


def test_已撤回的同意_核验报告现势状态(client, data):
    from app.database import SessionLocal
    from app.models import ConsentRecord
    from app.models._base import utcnow

    db = SessionLocal()
    record = db.get(ConsentRecord, data["consent"])
    record.revoked_at = utcnow()
    db.commit()
    db.close()
    token = make_verify_token(
        doc_type="consent", doc_id=data["consent"], doc_no=f"TY{data['consent']:08d}",
        org_name="验真测试医院", issued_date="2026-08-28",
    )
    body = client.get("/api/print/verify", params={"token": token}).json()
    assert body["valid"] is True, "纸是真的——撤回不改真伪，改的是现势状态"
    assert body["status"] == "已撤回"


def test_公开端点有限速(client, monkeypatch):
    monkeypatch.setattr(
        printing, "_verify_limiter", SlidingWindowRateLimiter(max_events=2, window_seconds=60)
    )
    for _ in range(2):
        assert client.get("/api/print/verify", params={"token": "x"}).status_code == 200
    assert client.get("/api/print/verify", params={"token": "x"}).status_code == 429


# ================================================================ 打印端点接线


def test_打印页带真二维码且编的是核验页地址(client, data, admin, monkeypatch):
    captured = []
    real = printing.qr_svg
    monkeypatch.setattr(printing, "qr_svg", lambda content: (captured.append(content) or real(content)))
    resp = client.get(f"/api/print/referrals/{data['referral']}", headers=admin)
    assert resp.status_code == 200
    assert "<svg" in resp.text and "扫码验真" in resp.text
    assert "验真占位" not in resp.text, "占位框应已被真码替换"
    (url,) = captured
    # 编页面地址而非 API；令牌放 # 片段（不进服务端访问日志）
    assert "/verify#v1." in url
    token = url.split("/verify#", 1)[1]
    body = client.get("/api/print/verify", params={"token": token}).json()
    assert body["valid"] is True and body["doc_type"] == "referral"


def test_模板关闭二维码则不出码(client, data, admin):
    client.put(
        "/api/print/templates",
        json={"doc_type": "referral", "header_org_name": "", "footer_note": "", "show_qr": False},
        headers=admin,
    )
    resp = client.get(f"/api/print/referrals/{data['referral']}", headers=admin)
    assert "<svg" not in resp.text and "扫码验真" not in resp.text
    client.put(
        "/api/print/templates",
        json={"doc_type": "referral", "header_org_name": "", "footer_note": "", "show_qr": True},
        headers=admin,
    )


def test_十二种单据的令牌都能核验存在性(client, data):
    """VERIFY_REGISTRY 的每个条目都要真的能按 id 取数——配错模型这里就炸。"""
    from app.database import SessionLocal

    db = SessionLocal()
    for doc_type in VERIFY_REGISTRY:
        token = make_verify_token(
            doc_type=doc_type, doc_id=88888888, doc_no="N", org_name="O", issued_date="2026-01-01",
        )
        body = verify_document(db, token)
        assert body["valid"] is False and "查不到" in body["reason"], doc_type
    db.close()


# ================================================================ 前端与 spd 委托


def test_核验页可达且从hash取令牌(client):
    resp = client.get("/verify")
    assert resp.status_code == 200
    assert "location.hash" in resp.text, "令牌约定在 # 片段里，页内必须从 hash 读"
    assert "/api/print/verify" in resp.text
    assert 'src="/static/shared.js"' in resp.text, "esc() 必须来自共享实现（CLAUDE.md §8）"


def test_spd委托与平台实现同源(client):
    from app.qrsvg import qr_svg
    from app.spd.routers.config._base import _qr_svg

    assert _qr_svg("https://example.com/x") == qr_svg("https://example.com/x")
