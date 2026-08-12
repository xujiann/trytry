"""站内消息：由真实业务事件产生，工作人员与居民两侧分别读取。

重点不在 CRUD，而在**消息确实由业务动作派生**——一张空的消息表毫无价值。
"""
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


def _login(client, username, password="passw0rd1"):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def setup(client, admin):
    town = client.post(
        "/api/organizations", json={"name": "消息演示卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    county = client.post(
        "/api/organizations", json={"name": "消息演示县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    # 申请机构的医师：危急值消息应当发给他
    client.post(
        "/api/users",
        json={"username": "msg_doc", "password": "passw0rd1", "full_name": "乡镇张医生",
              "role": "doctor", "org_id": town["id"]},
        headers=admin,
    )
    # 另一家机构的医师：不该收到别人机构的危急值
    client.post(
        "/api/users",
        json={"username": "msg_doc_other", "password": "passw0rd1", "full_name": "县院李医生",
              "role": "doctor", "org_id": county["id"]},
        headers=admin,
    )
    client.post(
        "/api/users",
        json={"username": "msg_dir", "password": "passw0rd1", "full_name": "主任",
              "role": "director", "org_id": town["id"]},
        headers=admin,
    )
    patient = client.post(
        "/api/patients",
        json={"name": "消息居民", "id_card": "331782199001011234", "phone": "13600009001"},
        headers=admin,
    ).json()
    return {
        "town": town, "county": county, "patient": patient,
        "doctor": _login(client, "msg_doc"),
        "other_doctor": _login(client, "msg_doc_other"),
        "director": _login(client, "msg_dir"),
    }


def _clear_cooldown(phone):
    from app.database import SessionLocal

    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()


@pytest.fixture(scope="module")
def resident(client, setup):
    """居民账户：手机号唯一命中患者，登录即自动实名绑定。"""
    _clear_cooldown("13600009001")
    code = client.post(
        "/api/portal/auth/sms/code", json={"phone": "13600009001"}
    ).json()["debug_code"]
    body = client.post(
        "/api/portal/auth/sms/login", json={"phone": "13600009001", "code": code}
    ).json()
    assert body["bound"] is True
    return {"Authorization": f"Bearer {body['access_token']}"}


def _order_exam(client, admin, setup, item_name="胸部DR"):
    return client.post(
        "/api/exams",
        json={"patient_id": setup["patient"]["id"], "from_org_id": setup["town"]["id"],
              "center_type": "imaging", "item_code": "DR-CHEST", "item_name": item_name},
        headers=admin,
    ).json()


# ---------------------------------------------------------------- 由业务事件派生


def test_critical_value_notifies_requesting_org_doctors_only(client, admin, setup):
    """危急值 → 申请机构的医师收到站内消息；别家机构的医师不该收到。"""
    req = _order_exam(client, admin, setup, "急诊血钾")
    client.post(f"/api/exams/{req['id']}/claim", headers=admin)
    client.post(
        f"/api/exams/{req['id']}/report",
        json={"finding": "血钾 7.1mmol/L", "conclusion": "重度高钾血症，立即处理",
              "critical": True, "reported_by": "检验科"},
        headers=admin,
    )
    mine = client.get("/api/notifications", headers=setup["doctor"]).json()
    critical = [n for n in mine if n["category"] == "critical_value"]
    assert critical, "申请机构医师应收到危急值站内消息"
    assert "重度高钾血症" in critical[0]["body"]
    assert critical[0]["link_type"] == "exam_report" and critical[0]["link_id"] > 0
    assert critical[0]["read"] is False

    others = client.get("/api/notifications", headers=setup["other_doctor"]).json()
    assert not [n for n in others if n["category"] == "critical_value"]


def test_normal_report_notifies_resident_not_staff(client, admin, setup, resident):
    """普通报告 → 通知患者本人；不该去打扰医师。"""
    before = len(client.get("/api/notifications", headers=setup["doctor"]).json())
    req = _order_exam(client, admin, setup, "腹部B超")
    client.post(f"/api/exams/{req['id']}/claim", headers=admin)
    client.post(
        f"/api/exams/{req['id']}/report",
        json={"finding": "肝胆胰脾未见异常", "conclusion": "未见明显异常",
              "critical": False, "reported_by": "超声科"},
        headers=admin,
    )
    msgs = client.get("/api/portal/me/notifications", headers=resident).json()
    reports = [n for n in msgs if n["category"] == "exam_report"]
    assert reports and "腹部B超" in reports[0]["title"]
    # 医师侧不增加
    assert len(client.get("/api/notifications", headers=setup["doctor"]).json()) == before


def test_surgery_schedule_notifies_resident(client, admin, setup, resident):
    ward = client.post(
        "/api/inpatient/wards", json={"org_id": setup["county"]["id"], "name": "消息病区"},
        headers=admin,
    ).json()
    bed = client.post(
        "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "M01"}, headers=admin
    ).json()
    adm = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": setup["patient"]["id"], "ward_id": ward["id"], "bed_id": bed["id"],
              "doctor_name": "外科医生", "diagnosis_name": "胆囊结石"},
        headers=admin,
    ).json()
    room = client.post(
        "/api/surgery/rooms", json={"org_id": setup["county"]["id"], "name": "消息手术间"},
        headers=admin,
    ).json()
    req = client.post(
        "/api/surgery/requests",
        json={"admission_id": adm["id"], "surgery_name": "腹腔镜胆囊切除术"},
        headers=setup["doctor"],
    ).json()
    client.post(
        f"/api/surgery/requests/{req['id']}/approve", json={"approved": True},
        headers=setup["director"],
    )
    client.post(
        f"/api/surgery/requests/{req['id']}/schedule",
        json={"room_id": room["id"], "scheduled_date": "2026-10-08",
              "start_time": "09:00", "end_time": "10:30"},
        headers=admin,
    )
    msgs = client.get("/api/portal/me/notifications", headers=resident).json()
    surgery = [n for n in msgs if n["category"] == "surgery"]
    assert surgery, "手术排班后患者应收到通知"
    assert "2026-10-08" in surgery[0]["body"] and "消息手术间" in surgery[0]["body"]


def test_discharge_notifies_resident_about_followup(client, admin, setup, resident):
    # 复用上一用例留下的在院记录——同一患者不能重复入院，这也是真实约束
    admissions = client.get(
        f"/api/inpatient/admissions?patient_id={setup['patient']['id']}", headers=admin
    ).json()
    adm = next(a for a in admissions if a["status"] == "admitted")
    client.post(
        f"/api/inpatient/admissions/{adm['id']}/case-summary",
        json={"discharge_diagnosis": "肺炎", "total_cost": 100, "drug_cost": 10, "outcome": "治愈"},
        headers=admin,
    )
    client.post(
        "/api/billing/settlements",
        json={"bill_type": "inpatient", "admission_id": adm["id"]}, headers=admin,
    )
    client.post(f"/api/inpatient/admissions/{adm['id']}/discharge", headers=admin)
    msgs = client.get("/api/portal/me/notifications", headers=resident).json()
    followups = [n for n in msgs if n["category"] == "followup" and "出院" in n["title"]]
    assert followups and followups[0]["link_type"] == "admission"


def test_family_manager_receives_child_notifications(client, admin, setup, resident):
    """代管人应收到被代管人的消息——只发给"本人账户"等于发进黑洞。"""
    child = client.post(
        "/api/patients", json={"name": "消息孩子", "id_card": "331782201901011234"}, headers=admin
    ).json()
    client.post(
        "/api/portal/me/family",
        json={"name": "消息孩子", "id_card": "331782201901011234", "relation": "child"},
        headers=resident,
    )
    req = client.post(
        "/api/exams",
        json={"patient_id": child["id"], "from_org_id": setup["town"]["id"],
              "center_type": "lab", "item_code": "CBC", "item_name": "儿童血常规"},
        headers=admin,
    ).json()
    client.post(f"/api/exams/{req['id']}/claim", headers=admin)
    client.post(
        f"/api/exams/{req['id']}/report",
        json={"finding": "白细胞轻度升高", "conclusion": "提示感染", "critical": False,
              "reported_by": "检验科"},
        headers=admin,
    )
    msgs = client.get("/api/portal/me/notifications", headers=resident).json()
    assert any("儿童血常规" in n["title"] for n in msgs)


# ---------------------------------------------------------------- 读取与已读


def test_unread_count_and_mark_read(client, setup):
    before = client.get("/api/notifications/unread-count", headers=setup["doctor"]).json()["unread"]
    assert before >= 1
    target = client.get("/api/notifications?unread_only=true", headers=setup["doctor"]).json()[0]
    assert client.post(
        f"/api/notifications/{target['id']}/read", headers=setup["doctor"]
    ).status_code == 200
    after = client.get("/api/notifications/unread-count", headers=setup["doctor"]).json()["unread"]
    assert after == before - 1
    # 重复标记不再扣减
    client.post(f"/api/notifications/{target['id']}/read", headers=setup["doctor"])
    assert client.get(
        "/api/notifications/unread-count", headers=setup["doctor"]
    ).json()["unread"] == after


def test_unread_sorted_first(client, admin, setup):
    """未读排在已读前面——否则收件箱越用越难找到新消息。"""
    _order_exam(client, admin, setup, "复查胸片")
    rows = client.get("/api/notifications", headers=setup["doctor"]).json()
    read_flags = [n["read"] for n in rows]
    assert read_flags == sorted(read_flags), "未读应排在已读之前"


def test_mark_all_read(client, admin, setup):
    req = _order_exam(client, admin, setup, "再来一张片子")
    client.post(f"/api/exams/{req['id']}/claim", headers=admin)
    client.post(
        f"/api/exams/{req['id']}/report",
        json={"finding": "x", "conclusion": "危急", "critical": True, "reported_by": "科室"},
        headers=admin,
    )
    assert client.get(
        "/api/notifications/unread-count", headers=setup["doctor"]
    ).json()["unread"] >= 1
    marked = client.post("/api/notifications/read-all", headers=setup["doctor"]).json()["marked"]
    assert marked >= 1
    assert client.get(
        "/api/notifications/unread-count", headers=setup["doctor"]
    ).json()["unread"] == 0
    # 已无未读时再次全标记为 0
    assert client.post(
        "/api/notifications/read-all", headers=setup["doctor"]
    ).json()["marked"] == 0


def test_cannot_read_others_notification(client, setup):
    """他人的消息按 404 处理，不暴露其存在。"""
    rows = client.get("/api/notifications", headers=setup["doctor"]).json()
    assert rows
    resp = client.post(f"/api/notifications/{rows[0]['id']}/read", headers=setup["other_doctor"])
    assert resp.status_code == 404


def test_resident_cannot_read_staff_notification(client, setup, resident):
    staff_msg = client.get("/api/notifications", headers=setup["doctor"]).json()[0]
    resp = client.post(f"/api/portal/me/notifications/{staff_msg['id']}/read", headers=resident)
    assert resp.status_code == 404


def test_resident_unread_count_and_read(client, resident):
    before = client.get(
        "/api/portal/me/notifications/unread-count", headers=resident
    ).json()["unread"]
    assert before >= 1
    target = client.get(
        "/api/portal/me/notifications?unread_only=true", headers=resident
    ).json()[0]
    client.post(f"/api/portal/me/notifications/{target['id']}/read", headers=resident)
    assert client.get(
        "/api/portal/me/notifications/unread-count", headers=resident
    ).json()["unread"] == before - 1


def test_notifications_require_auth(client):
    assert client.get("/api/notifications").status_code == 401
    assert client.get("/api/portal/me/notifications").status_code == 401
