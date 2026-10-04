"""术中死亡之后，同一次住院的其余手术照批照排、还能再提新申请（P2-1396，第四十一批「手术与麻醉闭环」扫描 AE2-3 的拦截一半）。

术中记录转归「死亡」原先只跳过术后随访与「术后随访已安排」（P2-499）；住院仍是「在院」（出院要先写病案首页）。修前实测：
同住院另一张已审批的申请照排 201，家属在居民端收到「手术已安排：二期肠造口还纳术 | … 请遵医嘱做好术前准备。」，那一台挂在
排班表与「待填术中记录」里占着手术间；待审批的照批 200；死后再提新申请 201。

修法：同一次住院已有转归「死亡」的术中记录，提新申请、批准、排班一律 409；驳回照旧放行（在途单收尾的出口，已审批 / 已排班
的怎么收尾随 P2-182）。判定按住院，不扩到患者主索引（P1-112 待裁定）。没死的照常。
"""
import pytest

from conftest import login

from app.database import SessionLocal
from app.models import Notification, SurgeryRequest, SurgerySchedule

S = "/api/surgery"
DIED = "本次住院患者已于手术中死亡（术中记录转归「死亡」）"


def _patient_with_portal(client, admin, name, id_card, phone):
    """建档并让本人在居民端绑上这份档案——绑了才看得出「手术已安排」发没发。"""
    patient = client.post("/api/patients", headers=admin, json={"name": name, "id_card": id_card, "phone": phone})
    assert patient.status_code in (200, 201), patient.text
    code = client.post("/api/portal/auth/sms/code", json={"phone": phone}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code}).json()["access_token"]
    bound = client.post("/api/portal/auth/realname", json={"name": name, "id_card": id_card},
                        headers={"Authorization": f"Bearer {token}"})
    assert bound.status_code in (200, 409), bound.text
    return patient.json()["id"]


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21396 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    # 申请人不得自批（职责分离）：申请与术中记录由本院医生来，审批、排班由管理员来
    resp = client.post("/api/users", headers=admin, json={
        "username": "p21396_doc", "password": "passw0rd1", "full_name": "P21396 医生", "role": "doctor", "org_id": org})
    assert resp.status_code in (200, 201), resp.text
    doctor = login(client, "p21396_doc", "passw0rd1")
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21396 外科"}).json()["id"]
    room = client.post(f"{S}/rooms", headers=admin, json={"org_id": org, "name": "P21396 手术间"}).json()["id"]
    admissions = {}
    for n, (key, id_card, phone) in enumerate((("died", "330102197007071396", "13900213961"),
                                               ("alive", "330102197008081396", "13900213962"))):
        patient = _patient_with_portal(client, admin, f"P21396 患者{key}", id_card, phone)
        bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": f"P21396-{n}"}).json()
        adm = client.post("/api/inpatient/admissions", headers=admin, json={
            "patient_id": patient, "ward_id": ward, "bed_id": bed["id"], "diagnosis_name": "肠梗阻"})
        assert adm.status_code == 201, adm.text
        admissions[key] = adm.json()["id"]
    return {"doctor": doctor, "room": room, **admissions}


def _request(client, world, admission, name):
    return client.post(f"{S}/requests", headers=world["doctor"], json={"admission_id": admission, "surgery_name": name})


def _approve(client, admin, rid, approved=True):
    return client.post(f"{S}/requests/{rid}/approve", headers=admin, json={"approved": approved})


def _schedule(client, admin, world, rid, day, start, end):
    return client.post(f"{S}/requests/{rid}/schedule", headers=admin, json={
        "room_id": world["room"], "scheduled_date": day, "start_time": start, "end_time": end})


def _first_surgery(client, admin, world, admission, outcome, start):
    """同住院先做的那一台：申请 → 审批 → 排班 → 术中记录（转归 `outcome`）。"""
    resp = _request(client, world, admission, f"P21396 剖腹探查术{outcome}")
    assert resp.status_code == 201, resp.text
    rid = resp.json()["id"]
    assert _approve(client, admin, rid).status_code == 200
    sched = _schedule(client, admin, world, rid, "2031-03-08", start, f"{int(start[:2]) + 2:02d}:00")
    assert sched.status_code == 201, sched.text
    rec = client.post(f"{S}/requests/{rid}/record", headers=world["doctor"], json={
        "actual_surgery_name": f"P21396 剖腹探查术{outcome}", "outcome": outcome})
    assert rec.status_code == 201, rec.text


@pytest.fixture(scope="module")
def died(client, admin, world):
    """同住院先提的三张：一张已审批（在途）、一张待审批（在途），第一台随后术中死亡。"""
    approved = _request(client, world, world["died"], "P21396 二期肠造口还纳术")
    pending = _request(client, world, world["died"], "P21396 气管切开术")
    assert approved.status_code == pending.status_code == 201
    assert _approve(client, admin, approved.json()["id"]).status_code == 200
    _first_surgery(client, admin, world, world["died"], "死亡", "13:00")
    return {"approved": approved.json()["id"], "pending": pending.json()["id"]}


def _arranged(rid):
    """这张申请的排班行数、给居民发的「手术已安排」条数、申请状态。"""
    with SessionLocal() as db:
        schedules = db.query(SurgerySchedule).filter(SurgerySchedule.request_id == rid).count()
        notices = db.query(Notification).filter(
            Notification.link_type == "surgery_request", Notification.link_id == rid,
            Notification.title.like("手术已安排%")).count()
        return schedules, notices, db.get(SurgeryRequest, rid).status


def test_死后排同住院已审批的那台_409_居民端不收手术已安排(client, admin, world, died):
    resp = _schedule(client, admin, world, died["approved"], "2031-03-28", "08:00", "10:00")
    assert resp.status_code == 409, resp.text   # 修前 201，家属收到「请遵医嘱做好术前准备」
    assert resp.json()["detail"] == f"{DIED}，不可排班"
    assert _arranged(died["approved"]) == (0, 0, "approved")


def test_死后批准同住院待审批的那张_409_驳回照旧放行(client, admin, world, died):
    resp = _approve(client, admin, died["pending"])
    assert resp.status_code == 409, resp.text   # 修前 200：批给死者，接着就能排班
    assert resp.json()["detail"] == f"{DIED}，不可审批通过"
    rejected = _approve(client, admin, died["pending"], approved=False)
    assert rejected.status_code == 200, rejected.text   # 驳回是在途单收尾的出口，别堵死
    assert rejected.json()["status"] == "cancelled"


def test_死后再提新申请_409(client, admin, world, died):
    resp = _request(client, world, world["died"], "P21396 再次剖腹探查术")
    assert resp.status_code == 409, resp.text   # 修前 201
    assert resp.json()["detail"] == f"{DIED}，不可再申请手术"
    with SessionLocal() as db:
        assert db.query(SurgeryRequest).filter(SurgeryRequest.surgery_name == "P21396 再次剖腹探查术").count() == 0


def test_没死的照常提申请_批准_排班_发手术已安排(client, admin, world):
    _first_surgery(client, admin, world, world["alive"], "好转", "08:00")
    resp = _request(client, world, world["alive"], "P21396 二期肠造口还纳术（好转）")
    assert resp.status_code == 201, resp.text
    rid = resp.json()["id"]
    assert _approve(client, admin, rid).status_code == 200
    sched = _schedule(client, admin, world, rid, "2031-03-29", "08:00", "10:00")
    assert sched.status_code == 201, sched.text
    assert _arranged(rid) == (1, 1, "scheduled")
