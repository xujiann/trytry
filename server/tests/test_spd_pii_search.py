"""spd 证件号检索接入 PII 加密态（P1-25）：关态既有行为回归，开态全值命中、模糊落空。

此前 spd 两处证件号 contains（care.py 上报明细、population.py 在管患者）受依赖
边界约束未接入 pii 模块——开态下密文列 contains 恒空，检索静默失明。本包把
`pii` 加进依赖白名单（经 platform.py 再导出 pii_filter）后：关态保持 contains
原行为；开态与平台 patients.py 同一降级口径——仅全值命中（索引列等值），
前缀/中缀模糊落空是**既定降级**而非 bug。
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import reset_database

from app.config import settings
from app.database import SessionLocal, engine
from app.main import app
from app.pii import PII_PREFIX
from app.spd.models import SpdCaseReport, SpdEnrollment


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "pii_encryption_enabled", True)
    yield
    monkeypatch.setattr(settings, "pii_encryption_enabled", False)


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "PII检索县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


def _seed(client, admin, org, name, id_card, program="hypertension"):
    """建患者（走 API，索引列由写入侧事件维护）+ 直插上报与纳管行。"""
    patient = client.post(
        "/api/patients", json={"name": name, "id_card": id_card}, headers=admin
    ).json()
    with SessionLocal() as db:
        db.add(SpdCaseReport(patient_id=patient["id"], program_code=program,
                             content="血压异常上报"))
        db.add(SpdEnrollment(patient_id=patient["id"], program_code=program,
                             org_id=org["id"]))
        db.commit()
    return patient


def _report_patients(client, admin, id_card) -> set[int]:
    resp = client.get("/api/spd/case-reports", params={"id_card": id_card}, headers=admin)
    assert resp.status_code == 200, resp.text
    return {r["patient_id"] for r in resp.json()}


def _enrollment_patients(client, admin, keyword) -> set[int]:
    resp = client.get("/api/spd/enrollments", params={"keyword": keyword}, headers=admin)
    assert resp.status_code == 200, resp.text
    return {r["patient_id"] for r in resp.json()}


def test_关态_证件号模糊检索既有行为不变(client, admin, org):
    p = _seed(client, admin, org, "关态检索甲", "330782199004040011")
    # 中缀模糊命中（contains 原行为）
    assert p["id"] in _report_patients(client, admin, "19900404001")
    assert p["id"] in _enrollment_patients(client, admin, "19900404001")
    # 全值同样命中；不相干串落空
    assert p["id"] in _report_patients(client, admin, "330782199004040011")
    assert p["id"] not in _report_patients(client, admin, "999999")


def test_开态_全值命中_模糊落空为既定降级(client, admin, org, enabled):
    p = _seed(client, admin, org, "开态检索乙", "330782199004040022")
    # 前置自证：库里确实是密文（否则本用例在测明文，什么都证不了）。
    # 用裸 SQL 读——ORM/Core 都会被 EncryptedPII 透明解密
    with engine.connect() as conn:
        stored = conn.execute(
            text("SELECT id_card FROM patients WHERE id = :i"), {"i": p["id"]}
        ).scalar_one()
    assert stored.startswith(PII_PREFIX)
    # 全值命中：pii_filter 走索引列等值
    assert p["id"] in _report_patients(client, admin, "330782199004040022")
    assert p["id"] in _enrollment_patients(client, admin, "330782199004040022")
    # 前缀/中缀落空：与平台 patients.py 同一模糊降级口径
    assert p["id"] not in _report_patients(client, admin, "19900404002")
    assert p["id"] not in _enrollment_patients(client, admin, "19900404002")
    # 姓名模糊不受降级影响（population.py 的 keyword 同时查姓名）
    assert p["id"] in _enrollment_patients(client, admin, "开态检索乙")


def test_开态_关态存量明文行仍可全值命中(client, admin, org, enabled):
    """混存期兼容：关态建的明文行（索引列已旁路维护），开态全值检索不丢。"""
    settings.pii_encryption_enabled = False  # 临时关掉造"存量明文"（enabled fixture 会兜底还原）
    try:
        plain = _seed(client, admin, org, "存量明文丙", "330782199004040033")
    finally:
        settings.pii_encryption_enabled = True
    assert plain["id"] in _report_patients(client, admin, "330782199004040033")
    assert plain["id"] in _enrollment_patients(client, admin, "330782199004040033")
