"""居民端在线服务：家庭成员代管、档案切换、自助预约、签约/账单/转诊查询。"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app
from app.models import SmsCode
from app.routers.portal import _reset_portal_failures
from app.sms import set_sms_provider


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_state():
    _reset_portal_failures()
    set_sms_provider(None)
    yield
    _reset_portal_failures()
    set_sms_provider(None)


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _clear_cooldown(phone: str) -> None:
    from app.database import SessionLocal

    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()


def send_code(client, phone: str, purpose: str = "login") -> str:
    _clear_cooldown(phone)
    resp = client.post("/api/portal/auth/sms/code", json={"phone": phone, "purpose": purpose})
    assert resp.status_code == 200, resp.text
    return resp.json()["debug_code"]


def login(client, phone: str) -> dict:
    code = send_code(client, phone)
    body = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code}).json()
    return {"Authorization": f"Bearer {body['access_token']}"}


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations", json={"name": "服务演示卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def me(client, admin):
    """本人：手机号 13700010001，登录即自动实名绑定。"""
    patient = client.post(
        "/api/patients",
        json={"name": "服务本人", "id_card": "330782198701011234", "phone": "13700010001"},
        headers=admin,
    ).json()
    return {"patient": patient, "headers": login(client, "13700010001")}


def grant_delegate(client, admin, patient_id):
    """窗口预登记家庭代管授权（P1-2 双因子加固）。

    无手机号档案的"姓名+身份证号"是单因子，纳管前必须先有一条窗口核验身份后
    录入的 ConsentRecord(scene=family_delegate)，否则 428（见 portal.py）。
    """
    resp = client.post(
        "/api/consents",
        json={"patient_id": patient_id, "scene": "family_delegate",
              "evidence": "窗口身份核验记录#TEST"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text


@pytest.fixture(scope="module")
def child(client, admin):
    """未留手机号的儿童档案：窗口已登记代管授权，可凭姓名+身份证号代管。"""
    patient = client.post(
        "/api/patients", json={"name": "服务孩子", "id_card": "330782201801012345"}, headers=admin
    ).json()
    grant_delegate(client, admin, patient["id"])
    return patient


# ---------------------------------------------------------------- 家庭成员代管


def test_family_list_contains_self(client, me):
    rows = client.get("/api/portal/me/family", headers=me["headers"]).json()
    assert len(rows) == 1
    assert rows[0]["is_self"] is True and rows[0]["relation"] == "self"
    assert rows[0]["patient_id"] == me["patient"]["id"]


def test_add_child_without_phone_needs_no_code(client, me, child):
    resp = client.post(
        "/api/portal/me/family",
        json={"name": "服务孩子", "id_card": "330782201801012345", "relation": "child"},
        headers=me["headers"],
    )
    assert resp.status_code == 201
    rows = client.get("/api/portal/me/family", headers=me["headers"]).json()
    assert {r["name"] for r in rows} == {"服务本人", "服务孩子"}


def test_add_duplicate_member_rejected(client, me, child):
    resp = client.post(
        "/api/portal/me/family",
        json={"name": "服务孩子", "id_card": "330782201801012345", "relation": "child"},
        headers=me["headers"],
    )
    assert resp.status_code == 409


def test_add_self_as_member_rejected(client, me):
    resp = client.post(
        "/api/portal/me/family",
        json={"name": "服务本人", "id_card": "330782198701011234"},
        headers=me["headers"],
    )
    assert resp.status_code == 409


def test_member_with_own_phone_requires_that_phone_code(client, admin, me):
    """被代管人自己留了手机号 → 光有姓名+身份证号不够，必须提供该号码的验证码。"""
    client.post(
        "/api/patients",
        json={"name": "服务配偶", "id_card": "330782198802022345", "phone": "13700010002"},
        headers=admin,
    )
    blocked = client.post(
        "/api/portal/me/family",
        json={"name": "服务配偶", "id_card": "330782198802022345", "relation": "spouse"},
        headers=me["headers"],
    )
    assert blocked.status_code == 428
    assert "137******02" in blocked.json()["detail"]

    code = send_code(client, "13700010002", purpose="bind")
    ok = client.post(
        "/api/portal/me/family",
        json={"name": "服务配偶", "id_card": "330782198802022345", "relation": "spouse", "code": code},
        headers=me["headers"],
    )
    assert ok.status_code == 201


def test_family_archive_switch_and_isolation(client, admin, me, child):
    """能查代管成员的档案，查未代管的他人档案则 403。"""
    mine = client.get("/api/portal/me/archive", headers=me["headers"]).json()
    assert mine["name"] == "服务本人"
    kid = client.get(
        f"/api/portal/me/archive?patient_id={child['id']}", headers=me["headers"]
    ).json()
    assert kid["name"] == "服务孩子"

    stranger = client.post(
        "/api/patients", json={"name": "无关路人", "id_card": "330782199909099876"}, headers=admin
    ).json()
    denied = client.get(
        f"/api/portal/me/archive?patient_id={stranger['id']}", headers=me["headers"]
    )
    assert denied.status_code == 403


def test_remove_family_member(client, admin, me):
    temp = client.post(
        "/api/patients", json={"name": "临时成员", "id_card": "330782199404043456"}, headers=admin
    ).json()
    grant_delegate(client, admin, temp["id"])
    added = client.post(
        "/api/portal/me/family",
        json={"name": "临时成员", "id_card": "330782199404043456", "relation": "other"},
        headers=me["headers"],
    ).json()
    assert client.delete(
        f"/api/portal/me/family/{added['member_id']}", headers=me["headers"]
    ).status_code == 200
    names = {r["name"] for r in client.get("/api/portal/me/family", headers=me["headers"]).json()}
    assert "临时成员" not in names


def test_cannot_remove_other_accounts_member(client, me, admin):
    """他人账户的代管关系删不掉（按 account_id 过滤而非仅按 id）。"""
    added = client.get("/api/portal/me/family", headers=me["headers"]).json()
    member_id = next(r["member_id"] for r in added if not r["is_self"])
    other = login(client, "13700010009")
    assert client.delete(
        f"/api/portal/me/family/{member_id}", headers=other
    ).status_code == 404


def test_unbound_account_cannot_add_family(client):
    headers = login(client, "13700010008")
    resp = client.post(
        "/api/portal/me/family",
        json={"name": "任意", "id_card": "330782199001019999"},
        headers=headers,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------- 自助预约


@pytest.fixture()
def slot(client, admin, org):
    return client.post(
        "/api/appointments/slots",
        json={"org_id": org["id"], "resource_type": "outpatient", "resource_name": "全科门诊",
              "slot_date": "2026-09-01", "slot_time": "09:00-10:00", "capacity": 2},
        headers=admin,
    ).json()


def test_portal_slots_only_lists_available(client, me, slot, admin, org):
    full = client.post(
        "/api/appointments/slots",
        json={"org_id": org["id"], "resource_type": "outpatient", "resource_name": "已满门诊",
              "slot_date": "2026-09-02", "slot_time": "09:00-10:00", "capacity": 1},
        headers=admin,
    ).json()
    client.post("/api/portal/me/appointments", json={"slot_id": full["id"]}, headers=me["headers"])
    rows = client.get("/api/portal/me/slots", headers=me["headers"]).json()
    names = {r["resource_name"] for r in rows}
    assert "全科门诊" in names and "已满门诊" not in names
    assert next(r for r in rows if r["resource_name"] == "全科门诊")["org_name"] == "服务演示卫生院"


def test_portal_book_for_self_and_child(client, me, child, slot):
    mine = client.post(
        "/api/portal/me/appointments", json={"slot_id": slot["id"]}, headers=me["headers"]
    )
    assert mine.status_code == 201
    kid = client.post(
        "/api/portal/me/appointments",
        json={"slot_id": slot["id"], "patient_id": child["id"]},
        headers=me["headers"],
    )
    assert kid.status_code == 201

    rows = client.get("/api/portal/me/appointments", headers=me["headers"]).json()
    booked = [r for r in rows if r["id"] in (mine.json()["id"], kid.json()["id"])]
    assert {r["patient_name"] for r in booked} == {"服务本人", "服务孩子"}


def test_portal_book_for_unmanaged_patient_denied(client, admin, me, slot):
    stranger = client.post(
        "/api/patients", json={"name": "他人甲", "id_card": "330782199505054567"}, headers=admin
    ).json()
    resp = client.post(
        "/api/portal/me/appointments",
        json={"slot_id": slot["id"], "patient_id": stranger["id"]},
        headers=me["headers"],
    )
    assert resp.status_code == 403


def test_portal_book_respects_capacity(client, me, child, slot, admin):
    """容量 2 已被本人与孩子占满，第三个成员预约应 409。"""
    client.post("/api/portal/me/appointments", json={"slot_id": slot["id"]}, headers=me["headers"])
    client.post(
        "/api/portal/me/appointments",
        json={"slot_id": slot["id"], "patient_id": child["id"]},
        headers=me["headers"],
    )
    third_patient = client.post(
        "/api/patients", json={"name": "第三人", "id_card": "330782199606065678"}, headers=admin
    ).json()
    grant_delegate(client, admin, third_patient["id"])
    client.post(
        "/api/portal/me/family",
        json={"name": "第三人", "id_card": "330782199606065678", "relation": "other"},
        headers=me["headers"],
    )
    third = client.get("/api/portal/me/family", headers=me["headers"]).json()
    pid = next(r["patient_id"] for r in third if r["name"] == "第三人")
    resp = client.post(
        "/api/portal/me/appointments", json={"slot_id": slot["id"], "patient_id": pid},
        headers=me["headers"],
    )
    assert resp.status_code == 409


def test_portal_cancel_releases_slot(client, me, slot, admin):
    booked = client.post(
        "/api/portal/me/appointments", json={"slot_id": slot["id"]}, headers=me["headers"]
    ).json()
    before = client.get(f"/api/appointments/slots?org_id={slot['org_id']}", headers=admin).json()
    used = next(s for s in before if s["id"] == slot["id"])["booked"]
    resp = client.post(
        f"/api/portal/me/appointments/{booked['id']}/cancel", headers=me["headers"]
    )
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
    after = client.get(f"/api/appointments/slots?org_id={slot['org_id']}", headers=admin).json()
    assert next(s for s in after if s["id"] == slot["id"])["booked"] == used - 1


def test_portal_cannot_cancel_others_appointment(client, admin, me, slot):
    """他人的预约在居民端表现为 404（不泄露是否存在）。"""
    stranger = client.post(
        "/api/patients", json={"name": "他人乙", "id_card": "330782199707076789"}, headers=admin
    ).json()
    other = client.post(
        "/api/appointments",
        json={"slot_id": slot["id"], "patient_id": stranger["id"]},
        headers=admin,
    ).json()
    resp = client.post(f"/api/portal/me/appointments/{other['id']}/cancel", headers=me["headers"])
    assert resp.status_code == 404


# ---------------------------------------------------------------- 我的服务


def test_portal_my_contract(client, admin, me, org):
    contract = client.post(
        "/api/contracts",
        json={"patient_id": me["patient"]["id"], "org_id": org["id"], "doctor_name": "王家医",
              "package": "standard", "signed_date": "2026-01-05"},
        headers=admin,
    ).json()
    client.post(
        f"/api/contracts/{contract['id']}/services",
        json={"service_type": "visit", "note": "上门测血压"},
        headers=admin,
    )
    rows = client.get("/api/portal/me/contract", headers=me["headers"]).json()
    assert rows[0]["doctor_name"] == "王家医"
    assert rows[0]["org_name"] == "服务演示卫生院"
    assert rows[0]["services"][0]["service_type"] == "visit"


def test_portal_my_referrals(client, admin, me, org):
    county = client.post(
        "/api/organizations", json={"name": "服务演示县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    client.post(
        "/api/referrals",
        json={"patient_id": me["patient"]["id"], "from_org_id": org["id"], "to_org_id": county["id"],
              "direction": "up", "reason": "血压控制不佳"},
        headers=admin,
    )
    rows = client.get("/api/portal/me/referrals", headers=me["headers"]).json()
    assert rows[0]["to_org"] == "服务演示县医院"
    assert rows[0]["status"] == "pending"


def test_portal_my_bills(client, admin, me, org):
    enc = client.post(
        "/api/encounters",
        json={"patient_id": me["patient"]["id"], "org_id": org["id"], "doctor_name": "门诊医生",
              "diagnosis_name": "上呼吸道感染"},
        headers=admin,
    ).json()
    client.post(
        "/api/billing/charge-items",
        json={"code": "REG-FEE", "name": "普通门诊挂号费", "category": "other", "price": 10},
        headers=admin,
    )
    client.post(
        "/api/billing/details",
        json={"patient_id": me["patient"]["id"], "encounter_id": enc["id"], "item_code": "REG-FEE", "quantity": 2},
        headers=admin,
    )
    client.post(
        "/api/billing/settlements",
        json={"bill_type": "outpatient", "encounter_id": enc["id"], "insurance_pay": 8},
        headers=admin,
    )
    rows = client.get("/api/portal/me/bills", headers=me["headers"]).json()
    assert rows[0]["total_amount"] == 20
    assert rows[0]["self_pay"] == 12
    assert rows[0]["paid"] is False


def test_service_endpoints_require_login(client):
    for path in ("/api/portal/me/family", "/api/portal/me/appointments",
                 "/api/portal/me/contract", "/api/portal/me/bills", "/api/portal/me/referrals"):
        assert client.get(path).status_code == 401


# ---------------------------------------------------------------- 住院与手术（居民视角）


@pytest.fixture(scope="module")
def inpatient_setup(client, admin, org, me):
    """一次完整住院：入院 → 计费 → 结算，并排一台手术。"""
    ward = client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "居民端病区"}, headers=admin
    ).json()
    bed = client.post(
        "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "P01"}, headers=admin
    ).json()
    adm = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": me["patient"]["id"], "ward_id": ward["id"], "bed_id": bed["id"],
              "doctor_name": "住院王医生", "diagnosis_name": "急性阑尾炎"},
        headers=admin,
    ).json()
    for code, name, category, price in [("PB-BED", "床位费/日", "bed", 45),
                                        ("PB-DRUG", "注射用抗菌药", "drug", 62)]:
        client.post(
            "/api/billing/charge-items",
            json={"code": code, "name": name, "category": category, "price": price},
            headers=admin,
        )
    client.post(
        "/api/billing/details",
        json={"patient_id": me["patient"]["id"], "admission_id": adm["id"],
              "item_code": "PB-BED", "quantity": 4},
        headers=admin,
    )
    client.post(
        "/api/billing/details",
        json={"patient_id": me["patient"]["id"], "admission_id": adm["id"],
              "item_code": "PB-DRUG", "quantity": 3},
        headers=admin,
    )
    room = client.post(
        "/api/surgery/rooms", json={"org_id": org["id"], "name": "居民端手术间"}, headers=admin
    ).json()
    req = client.post(
        "/api/surgery/requests",
        json={"admission_id": adm["id"], "surgery_name": "腹腔镜阑尾切除术", "urgency": "urgent"},
        headers=admin,
    ).json()
    # 申请人不得自批，审批要换一个人（职责分离）
    client.post(
        "/api/users",
        json={"username": "portal_dir", "password": "passw0rd1", "full_name": "居民端主任",
              "role": "director"},
        headers=admin,
    )
    dir_token = client.post(
        "/api/auth/login", json={"username": "portal_dir", "password": "passw0rd1"}
    ).json()["access_token"]
    director = {"Authorization": f"Bearer {dir_token}"}
    approved = client.post(
        f"/api/surgery/requests/{req['id']}/approve", json={"approved": True}, headers=director
    )
    assert approved.status_code == 200, approved.text
    scheduled = client.post(
        f"/api/surgery/requests/{req['id']}/schedule",
        json={"room_id": room["id"], "scheduled_date": "2026-09-20",
              "start_time": "09:00", "end_time": "10:30"},
        headers=admin,
    )
    assert scheduled.status_code == 201, scheduled.text
    return {"admission": adm, "surgery": req, "ward": ward}


def test_portal_my_admissions(client, me, inpatient_setup):
    rows = client.get("/api/portal/me/admissions", headers=me["headers"]).json()
    row = next(r for r in rows if r["id"] == inpatient_setup["admission"]["id"])
    assert row["org_name"] == "服务演示卫生院"
    assert row["ward_name"] == "居民端病区" and row["bed_no"] == "P01"
    assert row["status"] == "admitted"
    assert row["days"] >= 1  # 当日入当日出计 1 天，与成本核算口径一致
    assert row["settled"] is False  # 有未结清明细


def test_portal_admission_bill_groups_by_category(client, me, inpatient_setup):
    adm_id = inpatient_setup["admission"]["id"]
    bill = client.get(f"/api/portal/me/admissions/{adm_id}/bill", headers=me["headers"]).json()
    assert bill["total_amount"] == 45 * 4 + 62 * 3
    assert bill["by_category"] == {"bed": 180.0, "drug": 186.0}
    assert len(bill["items"]) == 2
    assert all(i["settled"] is False for i in bill["items"])
    assert bill["settlements"] == []


def test_portal_bill_reflects_settlement(client, admin, me, inpatient_setup):
    adm_id = inpatient_setup["admission"]["id"]
    client.post(
        "/api/billing/settlements",
        json={"bill_type": "inpatient", "admission_id": adm_id, "insurance_pay": 200},
        headers=admin,
    )
    bill = client.get(f"/api/portal/me/admissions/{adm_id}/bill", headers=me["headers"]).json()
    assert len(bill["settlements"]) == 1
    assert bill["settlements"][0]["insurance_pay"] == 200
    assert bill["settlements"][0]["self_pay"] == bill["total_amount"] - 200
    assert all(i["settled"] for i in bill["items"])

    rows = client.get("/api/portal/me/admissions", headers=me["headers"]).json()
    assert next(r for r in rows if r["id"] == adm_id)["settled"] is True


def test_portal_bill_of_others_admission_is_404(client, admin, me, org, inpatient_setup):
    """他人的住院单在居民端表现为 404，不区分"不存在"与"不是你的"。"""
    other = client.post(
        "/api/patients", json={"name": "他人住院", "id_card": "330782199808081234"}, headers=admin
    ).json()
    bed = client.post(
        "/api/inpatient/beds", json={"ward_id": inpatient_setup["ward"]["id"], "bed_no": "P02"},
        headers=admin,
    ).json()
    other_adm = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": other["id"], "ward_id": inpatient_setup["ward"]["id"],
              "bed_id": bed["id"], "doctor_name": "医生", "diagnosis_name": "肺炎"},
        headers=admin,
    ).json()
    resp = client.get(f"/api/portal/me/admissions/{other_adm['id']}/bill", headers=me["headers"])
    assert resp.status_code == 404


def test_portal_my_surgeries_shows_schedule_not_operative_notes(client, me, inpatient_setup):
    """居民端只回术式与时间地点；术中记录属专业文书，不直接推给患者。"""
    rows = client.get("/api/portal/me/surgeries", headers=me["headers"]).json()
    row = next(r for r in rows if r["id"] == inpatient_setup["surgery"]["id"])
    assert row["surgery_name"] == "腹腔镜阑尾切除术"
    assert row["status"] == "scheduled"
    assert row["scheduled_date"] == "2026-09-20"
    assert row["scheduled_time"] == "09:00-10:30"
    assert row["room_name"] == "居民端手术间"
    # 不应出现术中记录的字段
    for leaked in ("findings", "blood_loss_ml", "complications", "procedure"):
        assert leaked not in row


def test_portal_inpatient_endpoints_respect_family_scope(client, admin, me, child):
    """代管成员的住院与手术可查，未代管的他人 403。"""
    assert client.get(
        f"/api/portal/me/admissions?patient_id={child['id']}", headers=me["headers"]
    ).status_code == 200
    stranger = client.post(
        "/api/patients", json={"name": "住院无关人", "id_card": "330782199909091234"},
        headers=admin,
    ).json()
    assert client.get(
        f"/api/portal/me/surgeries?patient_id={stranger['id']}", headers=me["headers"]
    ).status_code == 403


def test_portal_inpatient_requires_login(client):
    assert client.get("/api/portal/me/admissions").status_code == 401
    assert client.get("/api/portal/me/surgeries").status_code == 401
