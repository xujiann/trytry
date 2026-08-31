"""居民端调阅留痕（TECH_DEBT P1-1）：patient 数据读端点必须落 AccessLog。

审计原话：「居民端零 AccessLog，家庭代管调阅他人档案完全无痕」。本文件钉住：

1. 本人读自己档案 → 一条 AccessLog，主体 `resident:{account_id}`（与
   AuditLog 的居民主体口径一字不差，见 main._write_audit）、`user_id`/`org_id`
   为空（居民不在 users 表、不挂机构）、依据 `self`；
2. **家庭代管读他人档案**（第一优先场景）→ 留痕能看出「谁代管的（主体是
   代管人账户）、读的是谁（patient_id 是被代管人）、凭什么（basis=delegate）」；
3. 各读端点各自带说明数据类别的 resource 词（archive/consent/bill/…，
   spd 侧沿用业务端既有的 spd_ 前缀词表）；
4. 留痕失败**不拖垮读请求**（独立会话 + 吞异常，与业务端 visibility 同一降级）；
5. 写端点**不落读留痕**（写操作由审计中间件按同一居民主体落 AuditLog）；
6. 留痕是副作用不是出参：被改端点的响应体逐键与改前一致（本文件的
   特征化用例在改动前先跑绿过一遍，见各用例 docstring）。
"""
import pytest

from app.database import SessionLocal
from app.models import (
    AccessLog,
    Admission,
    Bed,
    ChronicPatient,
    Encounter,
    ExamReport,
    ExamRequest,
    Settlement,
    SmsCode,
    Ward,
)
from app.routers.portal import _reset_portal_failures
from app.sms import set_sms_provider


@pytest.fixture(autouse=True)
def clean_state():
    _reset_portal_failures()
    set_sms_provider(None)
    yield
    _reset_portal_failures()
    set_sms_provider(None)


def _clear_cooldown(phone: str) -> None:
    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()


def login(client, phone: str) -> dict:
    _clear_cooldown(phone)
    code = client.post(
        "/api/portal/auth/sms/code", json={"phone": phone, "purpose": "login"}
    ).json()["debug_code"]
    body = client.post(
        "/api/portal/auth/sms/login", json={"phone": phone, "code": code}
    ).json()
    return {"Authorization": f"Bearer {body['access_token']}"}


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "留痕县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def me(client, admin):
    """本人：手机号唯一命中 → 登录即自动实名绑定。"""
    patient = client.post(
        "/api/patients",
        json={"name": "留痕本人", "id_card": "330782198801013210", "phone": "13760010001"},
        headers=admin,
    ).json()
    headers = login(client, "13760010001")
    account_id = client.get("/api/portal/me", headers=headers).json()["account_id"]
    with SessionLocal() as db:
        from app.models import Patient

        ehc_no = db.get(Patient, patient["id"]).ehc_no
    return {"patient": patient, "headers": headers, "account_id": account_id, "ehc_no": ehc_no}


@pytest.fixture(scope="module")
def child(client, admin, me):
    """无手机号儿童档案：窗口代管授权后被本人纳管——代管读他档案的主角。"""
    patient = client.post(
        "/api/patients",
        json={"name": "留痕孩子", "id_card": "330782201801014321"},
        headers=admin,
    ).json()
    resp = client.post(
        "/api/consents",
        json={"patient_id": patient["id"], "scene": "family_delegate",
              "evidence": "窗口身份核验记录#ACLOG"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(
        "/api/portal/me/family",
        json={"name": "留痕孩子", "id_card": "330782201801014321", "relation": "child"},
        headers=me["headers"],
    )
    assert resp.status_code == 201, resp.text
    return patient


@pytest.fixture(scope="module")
def seeded(client, admin, org, me, child):
    """就诊/报告/慢病/住院/账单各一份，让读端点有数据可照、留痕有患者可记。"""
    with SessionLocal() as db:
        db.add(Encounter(patient_id=me["patient"]["id"], org_id=org["id"],
                         doctor_name="李医生", encounter_type="outpatient",
                         diagnosis_code="I10", diagnosis_name="高血压", summary="血压偏高"))
        req = ExamRequest(patient_id=me["patient"]["id"], from_org_id=org["id"],
                          center_type="imaging", item_code="CT01", item_name="胸部CT",
                          status="reported", created_by=1)
        db.add(req)
        db.flush()
        db.add(ExamReport(request_id=req.id, conclusion="未见明显异常", critical=False))
        db.add(ChronicPatient(patient_id=me["patient"]["id"], disease="hypertension",
                              level=1, managed_by_org_id=org["id"], next_due="2026-09-01"))
        db.add(Settlement(patient_id=me["patient"]["id"], org_id=org["id"],
                          bill_type="outpatient", total_amount=200, insurance_pay=120,
                          self_pay=80, created_by=1))
        ward = Ward(org_id=org["id"], name="留痕病区")
        db.add(ward)
        db.flush()
        bed = Bed(ward_id=ward.id, bed_no="7-1", status="occupied")
        db.add(bed)
        db.flush()
        adm_me = Admission(patient_id=me["patient"]["id"], org_id=org["id"],
                           ward_id=ward.id, bed_id=bed.id, doctor_name="赵主任",
                           diagnosis_name="冠心病", status="in_hospital", created_by=1)
        adm_kid = Admission(patient_id=child["id"], org_id=org["id"],
                            ward_id=ward.id, bed_id=bed.id, doctor_name="赵主任",
                            diagnosis_name="肺炎", status="in_hospital", created_by=1)
        db.add_all([adm_me, adm_kid])
        db.commit()
        return {"adm_me": adm_me.id, "adm_kid": adm_kid.id}


def _clear_access_logs() -> None:
    with SessionLocal() as db:
        db.query(AccessLog).delete()
        db.commit()


def _access_rows(**filters) -> list[dict]:
    with SessionLocal() as db:
        query = db.query(AccessLog).order_by(AccessLog.id)
        for key, value in filters.items():
            query = query.filter(getattr(AccessLog, key) == value)
        return [
            {"user_id": r.user_id, "username": r.username, "org_id": r.org_id,
             "patient_id": r.patient_id, "resource": r.resource, "basis": r.basis}
            for r in query.all()
        ]


# ---------------------------------------------------------------- 核心口径


def test_本人读自己档案落一条留痕主体是resident账户(client, me, seeded):
    _clear_access_logs()
    resp = client.get("/api/portal/me/archive", headers=me["headers"])
    assert resp.status_code == 200, resp.text
    rows = _access_rows()
    assert len(rows) == 1, rows
    assert rows[0] == {
        "user_id": None,  # 居民不在 users 表，主体不落 users 外键
        "username": f"resident:{me['account_id']}",  # 与 AuditLog 的居民主体同口径
        "org_id": None,  # 居民账号不挂机构（AccessLog 建表时已按此设计为可空）
        "patient_id": me["patient"]["id"],
        "resource": "archive",
        "basis": "self",
    }


def test_家庭代管读他人档案留痕区分代管人与被读人(client, me, child, seeded):
    """第一优先场景：主体是代管人账户，patient_id 是被代管人，依据 delegate。"""
    _clear_access_logs()
    resp = client.get(
        f"/api/portal/me/archive?patient_id={child['id']}", headers=me["headers"]
    )
    assert resp.status_code == 200, resp.text
    rows = _access_rows()
    assert len(rows) == 1, rows
    assert rows[0]["username"] == f"resident:{me['account_id']}"  # 谁在读：代管人
    assert rows[0]["patient_id"] == child["id"]  # 读的是谁：被代管人
    assert rows[0]["basis"] == "delegate"  # 凭什么：代管关系，与本人 self 分得开
    assert rows[0]["resource"] == "archive"


def test_越权读不落留痕(client, me, admin):
    """403 的访问没有"看到"任何档案——留痕记的是发生了的调阅，不是尝试。
    （尝试类事件由 LoginAudit/AuditLog 体系承担。）"""
    stranger = client.post(
        "/api/patients",
        json={"name": "留痕路人", "id_card": "330782199901015432"},
        headers=admin,
    ).json()
    _clear_access_logs()
    resp = client.get(
        f"/api/portal/me/archive?patient_id={stranger['id']}", headers=me["headers"]
    )
    assert resp.status_code == 403
    assert _access_rows() == []


# ---------------------------------------------------------------- 各读端点的词表


PORTAL_READS = [
    ("/api/portal/me/consents", "consent"),
    ("/api/portal/me/contract", "contract"),
    ("/api/portal/me/bills", "bill"),
    ("/api/portal/me/referrals", "referral"),
    ("/api/portal/me/referrals/all", "referral"),
    ("/api/portal/me/enrollments/all", "enrollment"),
    ("/api/portal/me/admissions", "admission"),
    ("/api/portal/me/surgeries", "surgery"),
]


@pytest.mark.parametrize("path,resource", PORTAL_READS)
def test_平台侧读端点各自落痕且可按patient切到代管成员(client, me, child, seeded, path, resource):
    _clear_access_logs()
    assert client.get(path, headers=me["headers"]).status_code == 200
    rows = _access_rows(resource=resource)
    assert len(rows) == 1, rows
    assert rows[0]["patient_id"] == me["patient"]["id"] and rows[0]["basis"] == "self"

    _clear_access_logs()
    assert client.get(f"{path}?patient_id={child['id']}", headers=me["headers"]).status_code == 200
    rows = _access_rows(resource=resource)
    assert len(rows) == 1, rows
    assert rows[0]["patient_id"] == child["id"] and rows[0]["basis"] == "delegate"
    assert rows[0]["username"] == f"resident:{me['account_id']}"


def test_我的预约列表覆盖到的每位患者各落一条(client, me, child, seeded):
    """预约列表一次返回本人 + 全部代管成员的记录，留痕逐人各记一条。"""
    _clear_access_logs()
    assert client.get("/api/portal/me/appointments", headers=me["headers"]).status_code == 200
    rows = _access_rows(resource="appointment")
    by_patient = {r["patient_id"]: r["basis"] for r in rows}
    assert by_patient == {me["patient"]["id"]: "self", child["id"]: "delegate"}


def test_代管调阅住院费用清单落痕(client, me, child, seeded):
    _clear_access_logs()
    resp = client.get(
        f"/api/portal/me/admissions/{seeded['adm_kid']}/bill", headers=me["headers"]
    )
    assert resp.status_code == 200, resp.text
    rows = _access_rows(resource="admission_bill")
    assert len(rows) == 1
    assert rows[0]["patient_id"] == child["id"] and rows[0]["basis"] == "delegate"


def test_押金流水落痕仅限本人(client, me, seeded):
    _clear_access_logs()
    resp = client.get(
        f"/api/portal/me/deposits?admission_id={seeded['adm_me']}", headers=me["headers"]
    )
    assert resp.status_code == 200, resp.text
    rows = _access_rows(resource="deposit")
    assert len(rows) == 1
    assert rows[0]["patient_id"] == me["patient"]["id"] and rows[0]["basis"] == "self"


SPD_READS = [
    ("/api/portal/spd/home", "spd_home"),
    ("/api/portal/spd/archive", "spd_archive"),
    ("/api/portal/spd/measurements", "spd_measurement"),
    ("/api/portal/spd/service-applies", "spd_apply"),
    ("/api/portal/spd/journey", "spd_journey"),
    ("/api/portal/spd/tasks", "spd_task"),
    ("/api/portal/spd/followups", "spd_followup"),
    ("/api/portal/spd/interventions", "spd_intervention"),
    ("/api/portal/spd/edu", "spd_edu"),
    ("/api/portal/spd/revisits", "spd_revisit"),
    ("/api/portal/spd/assessments", "spd_assessment"),
    ("/api/portal/spd/referrals", "spd_referral"),
    ("/api/portal/spd/consults", "spd_consult"),
]


@pytest.mark.parametrize("path,resource", SPD_READS)
def test_慢专病患者移动端读端点落痕(client, me, child, seeded, path, resource):
    """spd 居民端经 platform.accessible_patient 同一入口留痕（边界不破）。"""
    _clear_access_logs()
    assert client.get(path, headers=me["headers"]).status_code == 200
    rows = _access_rows(resource=resource)
    assert len(rows) == 1, rows
    assert rows[0]["basis"] == "self" and rows[0]["patient_id"] == me["patient"]["id"]

    _clear_access_logs()
    assert client.get(f"{path}?patient_id={child['id']}", headers=me["headers"]).status_code == 200
    rows = _access_rows(resource=resource)
    assert len(rows) == 1, rows
    assert rows[0]["basis"] == "delegate" and rows[0]["patient_id"] == child["id"]
    assert rows[0]["username"] == f"resident:{me['account_id']}"


def test_遗留双因子查档也留痕(client, me, monkeypatch):
    """过渡兼容通道（默认关）只要开着就同样查得到账：无账户体系，主体记
    portal:legacy，依据仍是 self（双因子核验的就是本人身份）。"""
    from app.config import settings

    monkeypatch.setattr(settings, "portal_legacy_verify", True)
    _clear_access_logs()
    resp = client.get(
        f"/api/portal/my-archive?ehc_no={me['ehc_no']}&id_card=330782198801013210"
    )
    assert resp.status_code == 200, resp.text
    rows = _access_rows()
    assert len(rows) == 1
    assert rows[0]["username"] == "portal:legacy"
    assert rows[0]["patient_id"] == me["patient"]["id"]
    assert rows[0]["resource"] == "archive" and rows[0]["basis"] == "self"


# ---------------------------------------------------------------- 边界与降级


def test_写端点不落读留痕(client, me, seeded):
    """写操作由审计中间件按 resident:{id} 落 AuditLog；AccessLog 只记读，
    混进写流量会稀释"谁看了谁的档案"这个问题的答案。"""
    _clear_access_logs()
    resp = client.post(
        "/api/portal/me/consents", json={"scene": "followup"}, headers=me["headers"]
    )
    assert resp.status_code == 201, resp.text
    assert _access_rows() == []


def test_留痕会话故障不拖垮读请求(client, me, seeded, monkeypatch):
    """独立会话开不出来（库抖动）→ 读请求照常 2xx，响应体分毫不差。"""
    baseline = client.get("/api/portal/me/archive", headers=me["headers"])
    assert baseline.status_code == 200

    import app.visibility as visibility

    def _boom():
        raise RuntimeError("留痕库炸了")

    monkeypatch.setattr(visibility, "SessionLocal", _boom)
    resp = client.get("/api/portal/me/archive", headers=me["headers"])
    assert resp.status_code == 200
    assert resp.json() == baseline.json()


def test_留痕落库故障不拖垮读请求(client, me, seeded, monkeypatch):
    """会话开得出来、写入时炸（约束/磁盘满）→ 同样吞掉，读请求照常 2xx。"""
    import app.visibility as visibility

    class _ExplodingLog:
        def __init__(self, **kwargs):
            raise RuntimeError("留痕写入炸了")

    monkeypatch.setattr(visibility, "AccessLog", _ExplodingLog)
    resp = client.get(
        "/api/portal/me/bills", headers=me["headers"]
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------- 响应字节不变


def test_留痕是副作用响应体逐键与改前一致_档案(client, me, seeded):
    """特征化：本用例在接留痕**之前**先对未改代码跑绿过（同一份断言），
    改后仍绿即证明响应逐键未变——留痕只是副作用，不是出参。"""
    from app.routers.chronic import guidance_for

    with SessionLocal() as db:
        guidance = guidance_for(db, "hypertension")
    resp = client.get("/api/portal/me/archive", headers=me["headers"])
    assert resp.status_code == 200
    assert resp.json() == {
        "name": "留痕本人",
        "ehc_no": me["ehc_no"],
        "encounters": [
            {"diagnosis_name": "高血压", "encounter_type": "outpatient", "summary": "血压偏高"}
        ],
        "exam_reports": [{"conclusion": "未见明显异常", "critical": False}],
        "chronic_care": [
            {"disease": "hypertension", "level": 1, "next_followup_due": "2026-09-01",
             "guidance_points": guidance}
        ],
    }


def test_留痕是副作用响应体逐键与改前一致_账单(client, me, org, seeded):
    """同上：账单端点逐键比对，顺带钉住 Money 列整数金额仍是 int 不被 float 化。"""
    with SessionLocal() as db:
        settlement = (
            db.query(Settlement)
            .filter(Settlement.patient_id == me["patient"]["id"])
            .one()
        )
        expected_date = settlement.created_at.date().isoformat()
        sid = settlement.id
    resp = client.get("/api/portal/me/bills", headers=me["headers"])
    assert resp.status_code == 200
    body = resp.json()
    assert body == [
        {"id": sid, "org_name": "留痕县医院", "bill_type": "outpatient",
         "total_amount": 200, "insurance_pay": 120, "self_pay": 80,
         "paid": False, "date": expected_date}
    ]
    assert isinstance(body[0]["total_amount"], int)  # 200 不是 200.0
