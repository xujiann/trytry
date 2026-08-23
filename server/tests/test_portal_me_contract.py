"""居民端 `me` 组 + 三个已废弃遗留端点的**响应契约**（22 个端点）。

这一批把 `app/routers/portal.py` 的契约欠账清零（696 → 674）。

要紧的三处判断，每一处都由本文件的用例钉住：

1. **`/me/family` 的条件键 `member_id`**：本人那一行没有它（本人不是一条代管
   关系）。声明成可选字段会给本人行注入 `"member_id": null`，客户端照着 null
   去调 `DELETE /me/family/None` 就是平白多出来的错误路径。故带
   `response_model_exclude_unset=True`，本文件钉住"本人行整个键不出现"。
2. **Money 陷阱**：`Settlement`/`BillDetail`/`Deposit` 的金额列都是
   `Money = Numeric(14,2, asdecimal=False)`，整数金额读回来是 **int**。声明成
   float 会把账单上的「200 元」变成「200.0 元」。种子里特意各造了整数与小数
   两种金额，两条都钉。
3. **`_build_archive` 只建模一次**：`/me/archive` 与两个已废弃的 `/my-archive`
   出自同一个 `_build_archive`，共用 `ArchiveOut`。本文件钉住三者形状一致——
   否则日后改一处漏两处。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import (
    Admission,
    Appointment,
    AppointmentSlot,
    Bed,
    BillDetail,
    ChargeItem,
    ChronicPatient,
    ContractService,
    Deposit,
    Encounter,
    ExamReport,
    ExamRequest,
    FamilyDoctorContract,
    Notification,
    OperatingRoom,
    Organization,
    Patient,
    PaymentOrder,
    Referral,
    ResidentAccount,
    ResidentFamilyMember,
    Settlement,
    SurgeryRequest,
    SurgerySchedule,
    Ward,
    utcnow,
)


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def seeded(client):
    """一份够把所有形状照出来的数据：金额各造整数与小数两种、手术一排一未排、
    消息一读一未读、家庭成员一位（用来验本人行与成员行的键集合不同）。"""
    with SessionLocal() as db:
        org = Organization(name="契约县医院", org_type="hospital", level="county")
        org2 = Organization(name="契约卫生院", org_type="clinic", level="town")
        db.add_all([org, org2])
        db.flush()

        me = Patient(ehc_no="EHC-C-001", name="契本人", id_card="330199199001011234",
                     gender="male", birth_date="1990-01-01", phone="13911102001")
        kid = Patient(ehc_no="EHC-C-002", name="契小孩", id_card="330199201501011234",
                      gender="female", birth_date="2015-01-01", phone="")
        db.add_all([me, kid])
        db.flush()

        acc = ResidentAccount(phone="13911102001", patient_id=me.id, nickname="小契",
                              wechat_openid="", status="active")
        db.add(acc)
        db.flush()
        db.add(ResidentFamilyMember(account_id=acc.id, patient_id=kid.id, relation="child"))

        db.add(Encounter(patient_id=me.id, org_id=org.id, doctor_name="李医生",
                         encounter_type="outpatient", diagnosis_code="I10",
                         diagnosis_name="高血压", summary="血压偏高"))
        req = ExamRequest(patient_id=me.id, from_org_id=org2.id, center_type="imaging",
                          item_code="CT01", item_name="胸部CT", status="reported", created_by=1)
        db.add(req)
        db.flush()
        db.add(ExamReport(request_id=req.id, conclusion="未见明显异常", critical=False))
        db.add(ChronicPatient(patient_id=me.id, disease="hypertension", level=1,
                              managed_by_org_id=org2.id, next_due="2026-09-01"))

        slot = AppointmentSlot(org_id=org.id, resource_type="doctor", resource_name="心内科",
                               slot_date="2026-09-10", slot_time="09:00", capacity=5, booked=0)
        slot2 = AppointmentSlot(org_id=org.id, resource_type="doctor", resource_name="呼吸科",
                                slot_date="2026-09-11", slot_time="10:00", capacity=3, booked=0)
        db.add_all([slot, slot2])
        db.flush()
        db.add(Appointment(slot_id=slot2.id, patient_id=me.id, status="booked"))

        ct = FamilyDoctorContract(patient_id=me.id, org_id=org2.id, doctor_name="王家医",
                                  package="basic", signed_date="2026-01-05", status="active")
        db.add(ct)
        db.flush()
        db.add(ContractService(contract_id=ct.id, service_type="followup", note="季度随访"))

        # 金额：一笔整数、一笔小数（Money 陷阱的两条分支）
        s1 = Settlement(patient_id=me.id, org_id=org.id, bill_type="outpatient",
                        total_amount=200, insurance_pay=120, self_pay=80, created_by=1)
        s2 = Settlement(patient_id=me.id, org_id=org.id, bill_type="outpatient",
                        total_amount=88.5, insurance_pay=50.25, self_pay=38.25, created_by=1)
        db.add_all([s1, s2])
        db.flush()
        db.add(PaymentOrder(settlement_id=s1.id, channel="wechat", amount=80,
                            status="paid", created_by=1))

        db.add(Referral(patient_id=me.id, from_org_id=org2.id, to_org_id=org.id,
                        direction="up", reason="血压控制不佳", status="accepted", created_by=1))

        ward = Ward(org_id=org.id, name="心内科病区")
        db.add(ward)
        db.flush()
        bed = Bed(ward_id=ward.id, bed_no="12-3", status="occupied")
        db.add(bed)
        db.flush()
        adm = Admission(patient_id=me.id, org_id=org.id, ward_id=ward.id, bed_id=bed.id,
                        doctor_name="赵主任", diagnosis_name="冠心病", status="in_hospital",
                        created_by=1)
        db.add(adm)
        db.flush()
        db.add_all([
            ChargeItem(code="CT-DRG", name="阿司匹林", category="drug", price=3, active=True),
            ChargeItem(code="CT-BED", name="床位费", category="bed", price=45.5, active=True),
        ])
        db.add_all([
            BillDetail(patient_id=me.id, admission_id=adm.id, item_code="CT-DRG",
                       item_name="阿司匹林", unit_price=3, quantity=10, amount=30, created_by=1),
            BillDetail(patient_id=me.id, admission_id=adm.id, item_code="CT-BED",
                       item_name="床位费", unit_price=45.5, quantity=2, amount=91, created_by=1),
        ])
        db.add(Deposit(admission_id=adm.id, amount=1000, deposit_type="prepay",
                       method="cash", operator="收费员甲"))

        room = OperatingRoom(org_id=org.id, name="1号手术间", active=True)
        db.add(room)
        db.flush()
        sr = SurgeryRequest(admission_id=adm.id, patient_id=me.id, org_id=org.id,
                            surgery_name="冠脉造影", urgency="elective",
                            planned_date="2026-09-15", status="scheduled", created_by=1)
        sr2 = SurgeryRequest(admission_id=adm.id, patient_id=me.id, org_id=org.id,
                             surgery_name="待排手术", urgency="elective",
                             planned_date="2026-09-20", status="requested", created_by=1)
        db.add_all([sr, sr2])
        db.flush()
        db.add(SurgerySchedule(request_id=sr.id, room_id=room.id, scheduled_date="2026-09-15",
                               start_time="08:00", end_time="10:00", created_by=1))

        db.add(Notification(resident_account_id=acc.id, category="report",
                            title="检查报告已出", body="您的胸部CT报告已出具",
                            link_type="exam", link_id=req.id))
        db.add(Notification(resident_account_id=acc.id, category="surgery",
                            title="手术已安排", body="", link_type="surgery",
                            link_id=sr.id, read_at=utcnow()))
        db.commit()
        return {"adm": adm.id, "slot": slot.id, "kid": kid.id, "org": org.id,
                "me": me.id, "phone": "13911102001"}


@pytest.fixture(scope="module")
def auth(client, seeded):
    code = client.post("/api/portal/auth/sms/code",
                       json={"phone": seeded["phone"], "purpose": "login"}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login",
                        json={"phone": seeded["phone"], "code": code}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------- 条件键：member_id
def test_家庭成员本人行整个没有member_id键而不是null(client, auth):
    """本批最要紧的守卫。

    去掉 `response_model_exclude_unset=True`，本人行会多出 `"member_id": null`
    ——客户端照着它调 `DELETE /me/family/None` 就是一条平白多出来的错误路径。
    """
    rows = client.get("/api/portal/me/family", headers=auth).json()
    me_row = next(r for r in rows if r["is_self"])
    other = next(r for r in rows if not r["is_self"])
    assert set(me_row) == {"patient_id", "name", "ehc_no", "relation", "is_self"}
    assert "member_id" not in me_row
    assert set(other) == {"patient_id", "name", "ehc_no", "relation", "is_self", "member_id"}
    assert isinstance(other["member_id"], int)


# ------------------------------------------------- Money：整数不得变小数
def test_账单整数金额仍是int小数金额仍是float(client, auth):
    bills = {b["total_amount"]: b for b in client.get("/api/portal/me/bills",
                                                      headers=auth).json()}
    assert 200 in bills and 88.5 in bills, bills.keys()
    whole = bills[200]
    assert whole["total_amount"] == 200 and isinstance(whole["total_amount"], int)
    assert whole["insurance_pay"] == 120 and isinstance(whole["insurance_pay"], int)
    assert whole["self_pay"] == 80 and isinstance(whole["self_pay"], int)
    frac = bills[88.5]
    assert isinstance(frac["insurance_pay"], float) and frac["insurance_pay"] == 50.25


def test_住院费用清单的每一处金额都保持int或float原样(client, auth, seeded):
    body = client.get(f"/api/portal/me/admissions/{seeded['adm']}/bill",
                      headers=auth).json()
    assert body["total_amount"] == 121 and isinstance(body["total_amount"], int)
    # 分类汇总是动态键（收费类别可配置），值同样不得被 float 化
    assert body["by_category"] == {"drug": 30, "bed": 91}
    assert all(isinstance(v, int) for v in body["by_category"].values())
    assert body["deposit_balance"] == 1000 and isinstance(body["deposit_balance"], int)
    by_name = {i["item_name"]: i for i in body["items"]}
    assert isinstance(by_name["阿司匹林"]["unit_price"], int)
    assert by_name["床位费"]["unit_price"] == 45.5


# ------------------------------------------------- 档案：三处共用一个形状
ARCHIVE_KEYS = {"name", "ehc_no", "encounters", "exam_reports", "chronic_care"}


def test_登录态档案的形状(client, auth):
    body = client.get("/api/portal/me/archive", headers=auth).json()
    assert set(body) == ARCHIVE_KEYS
    assert set(body["encounters"][0]) == {"diagnosis_name", "encounter_type", "summary"}
    assert set(body["exam_reports"][0]) == {"conclusion", "critical"}
    assert set(body["chronic_care"][0]) == {"disease", "level", "next_followup_due",
                                            "guidance_points"}
    assert isinstance(body["chronic_care"][0]["level"], int)


def test_两个已废弃的my_archive与登录态档案形状完全一致(client, auth):
    """三者共用 `_build_archive`，形状只建模一次（ArchiveOut）。

    这条用例的作用是：日后谁给其中一个加了字段而没管另外两个，这里当场转红。
    """
    old = settings.portal_legacy_verify
    settings.portal_legacy_verify = True
    try:
        token_body = client.get("/api/portal/me/archive", headers=auth).json()
        get_body = client.get(
            "/api/portal/my-archive",
            params={"ehc_no": "EHC-C-001", "id_card": "330199199001011234"},
        ).json()
        post_body = client.post(
            "/api/portal/my-archive",
            json={"ehc_no": "EHC-C-001", "id_card": "330199199001011234"},
        ).json()
    finally:
        settings.portal_legacy_verify = old
    assert get_body == post_body == token_body


def test_代管成员档案可查越权档案403(client, auth, seeded):
    ok = client.get("/api/portal/me/archive", headers=auth,
                    params={"patient_id": seeded["kid"]})
    assert ok.status_code == 200 and ok.json()["ehc_no"] == "EHC-C-002"
    assert client.get("/api/portal/me/archive", headers=auth,
                      params={"patient_id": 999999}).status_code == 403


# ------------------------------------------------- 其余各端点的键集合
def test_me的键集合与脱敏(client, auth):
    body = client.get("/api/portal/me", headers=auth).json()
    assert set(body) == {"account_id", "phone", "wechat_bound", "nickname", "bound",
                         "name", "ehc_no"}
    # 手机号出口脱敏（privacy.mask_phone），契约不得把它换成明文
    assert body["phone"] == "139******01"


def test_号源列表的键集合与余号(client, auth, seeded):
    rows = client.get("/api/portal/me/slots", headers=auth).json()
    assert rows and set(rows[0]) == {"id", "org_id", "org_name", "resource_type",
                                     "resource_name", "slot_date", "slot_time", "remaining"}
    filtered = client.get("/api/portal/me/slots", headers=auth,
                          params={"org_id": seeded["org"], "slot_date": "2026-09-10"}).json()
    assert [r["slot_date"] for r in filtered] == ["2026-09-10"]


def test_我的预约与签约与转诊的键集合(client, auth):
    appt = client.get("/api/portal/me/appointments", headers=auth).json()
    assert appt and set(appt[0]) == {"id", "patient_id", "patient_name", "org_name",
                                     "resource_name", "slot_date", "slot_time", "status"}
    contract = client.get("/api/portal/me/contract", headers=auth).json()
    assert contract and set(contract[0]) == {"id", "org_name", "doctor_name", "package",
                                             "signed_date", "status", "services"}
    assert set(contract[0]["services"][0]) == {"service_type", "note", "date"}
    ref = client.get("/api/portal/me/referrals", headers=auth).json()
    assert ref and set(ref[0]) == {"id", "direction", "from_org", "to_org", "reason",
                                   "status", "date"}


def test_住院列表在院时出院日期是空串而不是null(client, auth):
    rows = client.get("/api/portal/me/admissions", headers=auth).json()
    assert rows and set(rows[0]) == {"id", "org_name", "ward_name", "bed_no", "doctor_name",
                                     "diagnosis_name", "status", "admitted_date",
                                     "discharged_date", "days", "settled"}
    assert rows[0]["discharged_date"] == ""      # 在院中：空串，不是 null
    assert rows[0]["days"] >= 1


def test_手术列表未排期时三项是空串而不是null(client, auth):
    rows = {r["surgery_name"]: r for r in client.get("/api/portal/me/surgeries",
                                                     headers=auth).json()}
    assert set(rows["冠脉造影"]) == {"id", "surgery_name", "org_name", "surgeon_name",
                                     "urgency", "status", "planned_date", "scheduled_date",
                                     "scheduled_time", "room_name"}
    assert rows["冠脉造影"]["scheduled_time"] == "08:00-10:00"
    unscheduled = rows["待排手术"]
    assert unscheduled["scheduled_date"] == unscheduled["scheduled_time"] == ""
    assert unscheduled["room_name"] == ""


def test_消息列表复用notifications的既有契约(client, auth):
    """`portal` 与业务端消息出自同一个 `notification_out`，共用 `NotificationOut`
    ——不再另建第二份同形模型（CLAUDE.md §1.6 优先复用）。"""
    rows = client.get("/api/portal/me/notifications", headers=auth).json()
    assert rows and set(rows[0]) == {"id", "category", "title", "body", "link_type",
                                     "link_id", "read", "created_at"}
    unread = client.get("/api/portal/me/notifications", headers=auth,
                        params={"unread_only": True}).json()
    assert all(not n["read"] for n in unread)
    count = client.get("/api/portal/me/notifications/unread-count", headers=auth).json()
    assert set(count) == {"unread"} and count["unread"] == len(unread)


def test_越权与不存在的错误体不带契约字段(client, auth):
    """4xx 走 HTTPException，响应体是 `{"detail": ...}`，不受 response_model 影响。"""
    for resp in (
        client.get("/api/portal/me/admissions/999999/bill", headers=auth),
        client.post("/api/portal/me/notifications/999999/read", headers=auth),
        client.request("DELETE", "/api/portal/me/family/999999", headers=auth),
    ):
        assert resp.status_code == 404
        assert set(resp.json()) == {"detail"}


# ------------------------------------------------- 写侧端点
def test_预约与取消与标记已读与评价的响应形状(client, auth, seeded):
    booked = client.post("/api/portal/me/appointments", headers=auth,
                         json={"slot_id": seeded["slot"]})
    assert booked.status_code == 201
    assert set(booked.json()) == {"id", "slot_id", "status"}

    cancelled = client.post(
        f"/api/portal/me/appointments/{booked.json()['id']}/cancel", headers=auth)
    assert cancelled.status_code == 200 and set(cancelled.json()) == {"id", "status"}

    unread = [n for n in client.get("/api/portal/me/notifications",
                                    headers=auth).json() if not n["read"]]
    marked = client.post(f"/api/portal/me/notifications/{unread[0]['id']}/read", headers=auth)
    assert marked.status_code == 200
    assert marked.json() == {"id": unread[0]["id"], "read": True}

    survey = client.post("/api/portal/me/surveys", headers=auth,
                         json={"target_type": "encounter", "target_id": 1,
                               "score": 5, "comment": "很好"})
    assert survey.status_code == 201 and set(survey.json()) == {"id", "submitted"}
    assert survey.json()["submitted"] is True


def test_废弃的surveys与登录态surveys同形(client, auth):
    """两者共用 `SurveySubmittedOut`；本条钉住"同形"这件事本身。"""
    old = settings.portal_legacy_verify
    settings.portal_legacy_verify = True
    try:
        legacy = client.post("/api/portal/surveys",
                             json={"ehc_no": "EHC-C-001", "id_card": "330199199001011234",
                                   "target_type": "contract", "target_id": 1,
                                   "score": 4, "comment": "还行"})
    finally:
        settings.portal_legacy_verify = old
    assert legacy.status_code == 201
    assert set(legacy.json()) == {"id", "submitted"}


def test_家庭成员的增与删的响应形状(client, auth):
    """新增走 404 分支（查无此人）不改数据；删除放最后，删掉的是唯一一位成员。"""
    missing = client.post("/api/portal/me/family", headers=auth,
                          json={"name": "查无此人", "id_card": "330199190001019999"})
    assert missing.status_code == 404 and set(missing.json()) == {"detail"}

    rows = client.get("/api/portal/me/family", headers=auth).json()
    member_id = next(r["member_id"] for r in rows if not r["is_self"])
    removed = client.request("DELETE", f"/api/portal/me/family/{member_id}", headers=auth)
    assert removed.status_code == 200 and removed.json() == {"removed": True}


# ------------------------------------------------- 顺手修：押金金额被 float 化
def test_押金流水的整数金额不被float化(client, auth, seeded):
    """`/me/deposits` 的契约（a911f61 引入）把 `amount`/`balance` 声明成了 float，
    而 `Deposit.amount` 是 `Money` 列、`deposit_balance()` 也返回 int——
    1000 元的押金因此以 `1000.0` 出账。这是与本批同一类的 Money 陷阱，就近修掉。

    这条同时是回归网：改回 `float` 立刻转红。
    """
    body = client.get("/api/portal/me/deposits", headers=auth,
                      params={"admission_id": seeded["adm"]}).json()
    assert body["balance"] == 1000 and isinstance(body["balance"], int)
    assert body["items"][0]["amount"] == 1000
    assert isinstance(body["items"][0]["amount"], int)
