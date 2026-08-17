"""S17 数据质量闭环扩展：新表裸外键完整性对账。

E1–E9 / S1–S16 引入的松耦合整数引用（不建 DB 外键）会悬空。这里验证
`GET /api/dataquality/integrity-scan` 能扫出孤儿引用、且干净引用不误报：
- PaymentCase.admission_id：一条指向真实住院（不报），一条 999999（应报）；
- RefillRequest.prescription_id：指向不存在处方（应报）；
- ReferralClinicalRef.ref_id：exam_report 类指向缺失报告（应报）。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import (
    Admission,
    PaymentCase,
    RefillRequest,
    ReferralClinicalRef,
    User,
)


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "对账县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def patient(client, admin):
    return client.post(
        "/api/patients", json={"name": "对账患者", "id_card": "331782198802021234"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def seeded(client, admin, org, patient):
    """直接落库造出"干净 + 孤儿"两类松耦合引用。

    SQLite 默认不强制外键，故可直接插入指向 999999 / 888888 / 777777 的悬空引用，
    正是本对账要发现的现实脏数据形态。
    """
    db = SessionLocal()
    try:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        # 真实住院登记（ward/bed 引用不影响本对账，仅需 admissions.id 存在）
        adm = Admission(
            patient_id=patient["id"], org_id=org["id"], ward_id=1, bed_id=1,
            created_by=admin_id,
        )
        db.add(adm)
        db.flush()
        real_admission_id = adm.id

        # 干净病例：admission_id 指向真实住院，settlement 置空 → 不该被任何检查标记
        db.add(PaymentCase(
            admission_id=real_admission_id, org_id=org["id"], pay_mode="drg",
            group_code="G-CLEAN", insurance_settlement_id=None,
        ))
        # 孤儿病例：admission_id 指向不存在的 999999
        db.add(PaymentCase(
            admission_id=999999, org_id=org["id"], pay_mode="drg", group_code="G-ORPHAN",
        ))
        # 续方申请指向不存在处方 888888
        db.add(RefillRequest(
            patient_id=patient["id"], org_id=org["id"], prescription_id=888888,
        ))
        # 病历随转引用缺失的检查报告 777777
        db.add(ReferralClinicalRef(
            referral_id=1, ref_type="exam_report", ref_id=777777, created_by=admin_id,
        ))
        db.commit()
        return {"real_admission_id": real_admission_id}
    finally:
        db.close()


def _by_key(report):
    return {c["key"]: c for c in report["checks"]}


def test_未登录不可对账(client):
    assert client.get("/api/dataquality/integrity-scan").status_code == 401


def test_对账列出全部六个检查项(client, admin, seeded):
    report = client.get("/api/dataquality/integrity-scan", headers=admin).json()
    assert set(_by_key(report)) == {
        "payment_case_admission",
        "payment_case_settlement",
        "audit_flag_settlement",
        "refill_prescription",
        "refill_drug_code",
        "referral_clinical_ref",
    }


def test_孤儿住院被扫出而干净引用不误报(client, admin, seeded):
    checks = _by_key(client.get("/api/dataquality/integrity-scan", headers=admin).json())
    adm = checks["payment_case_admission"]
    # 只应有那条 admission_id=999999 的病例，指向真实住院的干净病例不得入列
    assert adm["orphan_count"] == 1
    assert len(adm["sample_ids"]) == 1


def test_孤儿处方与孤儿转诊引用被扫出(client, admin, seeded):
    checks = _by_key(client.get("/api/dataquality/integrity-scan", headers=admin).json())
    assert checks["refill_prescription"]["orphan_count"] == 1
    assert checks["referral_clinical_ref"]["orphan_count"] == 1
    assert checks["referral_clinical_ref"]["sample_ids"], "应回填孤儿样本 id"


def test_total_orphans等于各检查项之和(client, admin, seeded):
    report = client.get("/api/dataquality/integrity-scan", headers=admin).json()
    assert report["total_orphans"] == sum(c["orphan_count"] for c in report["checks"])
    # 至少覆盖了住院/处方/转诊三条孤儿
    assert report["total_orphans"] >= 3
