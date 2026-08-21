"""特征化网 + 契约：`GET /api/performance/orgs` 机构绩效计分卡。

按 `docs/接口标准与治理.md` 的配方：**先钉住现状、再加 response_model**，
加完网照样绿即证明响应字节没变（CLAUDE.md §11：治理不得改响应字节）。

这个接口的嵌套比多数接口更麻烦：

- `weights` 的**键是动态的**（来自 `performance_indicators` 表，表空时退回默认），
  所以只能是 `dict[str, float]`，不能逐个字段写死；
- `detail` 里**混着两种形状**——三段是 `{分子, 分母}` 小字典、两段是裸计数；
- `score` 是 `round(sum(...), 1)`。这里特意验过：`_normalized_weights` 在表空时
  退回非空的 `DEFAULT_INDICATORS`，`sum()` 永远在浮点上做，所以 `score` **恒为
  float**（`0.0` 而不是 `0`）。若它可能是 int，声明成 `float` 就会把 `0` 变成
  `0.0`——那是改响应字节，不是治理。

网里造了真实数据（转诊完成 1/2、远程诊断 1 次、慢病随访 1/2、处方合格 1/2、
家医服务 2 次）：全零的响应什么都钉不住，比率分母为 0 时代码走的是另一条分支。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import (
    ChronicPatient,
    ContractService,
    ExamRequest,
    FamilyDoctorContract,
    FollowUp,
    Organization,
    Patient,
    Prescription,
    Referral,
    User,
)


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def seeded(client, admin):
    """一家机构，五个维度各造出"部分完成"的数据，让每个比率都不是 0 也不是 1。

    机构与患者走接口建（`ehc_no` 是接口层生成的，直接插库会撞 NOT NULL），
    五个维度的业务数据直接落库——那几张表各有各的建单流程，绕开更省事也更稳。
    """
    org = client.post(
        "/api/organizations",
        json={"name": "绩效特征化院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    other = client.post(
        "/api/organizations",
        json={"name": "绩效对照院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "绩效患者", "id_card": "330281199505054512"},
        headers=admin,
    ).json()

    with SessionLocal() as db:
        org = db.get(Organization, org["id"])
        other = db.get(Organization, other["id"])
        patient = db.get(Patient, patient["id"])
        doctor = db.query(User).filter(User.username == "admin").first()

        # 转诊 2 条、完成 1 条 → referral 比率 0.5
        for status in ("completed", "pending"):
            db.add(Referral(
                patient_id=patient.id, from_org_id=org.id, to_org_id=other.id,
                direction="up", reason="绩效网", status=status, created_by=doctor.id,
            ))
        # 远程诊断已出报告 1 次 → volume_score(1, cap=5) = 0.2
        db.add(ExamRequest(
            patient_id=patient.id, from_org_id=org.id, center_type="imaging",
            item_code="CT", item_name="胸部CT", status="reported", created_by=doctor.id,
        ))
        # 慢病 2 人、随访到 1 人 → chronic 比率 0.5
        managed = []
        for disease in ("hypertension", "diabetes"):
            cp = ChronicPatient(patient_id=patient.id, disease=disease, managed_by_org_id=org.id)
            db.add(cp)
            managed.append(cp)
        db.commit()
        db.add(FollowUp(chronic_id=managed[0].id, sbp=130.0, dbp=80.0))
        # 处方 2 张、合格 1 张（auto_passed 计不计合格由参数控制）→ rx 比率 0.5
        for status in ("auto_passed", "rejected"):
            db.add(Prescription(
                patient_id=patient.id, org_id=org.id, diagnosis_name="高血压",
                status=status, created_by=doctor.id,
            ))
        # 家医签约服务 2 次 → volume_score(2, cap=5) = 0.4
        contract = FamilyDoctorContract(
            patient_id=patient.id, org_id=org.id, doctor_name="张医生", status="active",
        )
        db.add(contract)
        db.commit()
        db.refresh(contract)
        for service_type in ("visit", "followup"):
            db.add(ContractService(contract_id=contract.id, service_type=service_type))
        db.commit()
        return {"org_id": org.id, "other_id": other.id}


def _card(payload, org_id):
    return next(c for c in payload["scorecards"] if c["org_id"] == org_id)


# ---------------------------------------------------------------- 特征化：形状


def test_顶层只有weights与scorecards两段(client, admin, seeded):
    payload = client.get("/api/performance/orgs", headers=admin).json()
    assert set(payload) == {"weights", "scorecards"}


def test_weights键动态且值恒为float(client, admin, seeded):
    """键来自指标表（表空时退回默认），所以契约只能写 dict[str, float]。"""
    weights = client.get("/api/performance/orgs", headers=admin).json()["weights"]
    assert weights, "表空时应退回默认指标，不该是空字典"
    assert all(isinstance(v, float) for v in weights.values())
    assert abs(sum(weights.values()) - 100.0) < 0.05, "权重归一化到 100"


def test_计分卡字段集合与类型(client, admin, seeded):
    card = _card(client.get("/api/performance/orgs", headers=admin).json(), seeded["org_id"])
    assert set(card) == {"org_id", "org_name", "level", "score", "detail"}
    assert isinstance(card["org_id"], int)
    assert isinstance(card["org_name"], str)
    assert isinstance(card["level"], str)
    assert isinstance(card["score"], float), "score 必须是 float，int 会让契约改掉响应字节"
    assert not isinstance(card["score"], bool)


def test_detail的五段形状_三段是分子分母两段是裸计数(client, admin, seeded):
    detail = _card(
        client.get("/api/performance/orgs", headers=admin).json(), seeded["org_id"]
    )["detail"]
    assert set(detail) == {
        "referral_completion", "remote_exams", "chronic_followup",
        "rx_pass", "contract_services",
    }
    assert detail["referral_completion"] == {"completed": 1, "total": 2}
    assert detail["chronic_followup"] == {"followed": 1, "total": 2}
    assert detail["rx_pass"] == {"passed": 1, "total": 2}
    assert detail["remote_exams"] == 1
    assert detail["contract_services"] == 2


def test_无数据机构各段也齐全且为零(client, admin, seeded):
    """比率分母为 0 时走的是另一条分支，字段不能因此消失。"""
    card = _card(client.get("/api/performance/orgs", headers=admin).json(), seeded["other_id"])
    assert card["score"] == 0.0
    assert card["detail"]["referral_completion"] == {"completed": 0, "total": 0}
    assert card["detail"]["remote_exams"] == 0


# ---------------------------------------------------------------- 特征化：口径


def test_score按权重折算并保留一位小数(client, admin, seeded):
    """钉住折算值本身——重构分数公式时这条会立刻报警。

    默认权重 referral 20 / remote_exam 20 / chronic 25 / rx 20 / contract 15，
    比率 0.5 / 0.2 / 0.5 / 0.5 / 0.4 → 10 + 4 + 12.5 + 10 + 6 = 42.5
    """
    card = _card(client.get("/api/performance/orgs", headers=admin).json(), seeded["org_id"])
    assert card["score"] == 42.5


def test_include_auto_passed收紧口径会降低rx段(client, admin, seeded):
    """传 False 时只认药师人工审核通过——本网里 auto_passed 那张不再算合格。"""
    payload = client.get(
        "/api/performance/orgs?include_auto_passed=false", headers=admin
    ).json()
    card = _card(payload, seeded["org_id"])
    assert card["detail"]["rx_pass"] == {"passed": 0, "total": 2}
    assert card["score"] == 32.5, "rx 段 20 分归零，总分从 42.5 降到 32.5"


def test_volume_cap改变量类维度折算(client, admin, seeded):
    """cap=1 时远程诊断 1 次即满分，家医 2 次也封顶。"""
    payload = client.get("/api/performance/orgs?volume_cap=1", headers=admin).json()
    card = _card(payload, seeded["org_id"])
    assert card["score"] == 67.5, "remote_exam 与 contract 两段满分：10+20+12.5+10+15"


def test_按分数倒序(client, admin, seeded):
    scores = [c["score"] for c in client.get("/api/performance/orgs", headers=admin).json()["scorecards"]]
    assert scores == sorted(scores, reverse=True)


def test_volume_cap非法拒绝(client, admin, seeded):
    assert client.get("/api/performance/orgs?volume_cap=0", headers=admin).status_code == 422
