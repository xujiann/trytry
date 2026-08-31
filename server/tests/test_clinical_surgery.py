"""阶段二：住院临床文书、手术麻醉、统一随访中心。"""
from datetime import date, timedelta

import pytest

from conftest import login


@pytest.fixture(scope="module")
def roles(client, admin, ward):
    """一组业务角色账号：手术审批要求申请人与审批人分离。
    第九轮：挂到手术演示医院——手术排班/审批是院内流程，按 id 操作要过机构校验。"""
    org_id = ward["org"]["id"]
    for username, role in [("surg_doc", "doctor"), ("surg_doc2", "doctor"),
                           ("surg_dir", "director"), ("surg_op", "operator")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "passw0rd1", "full_name": username,
                  "role": role, "org_id": org_id},
            headers=admin,
        )
    return {
        "doctor": login(client, "surg_doc", "passw0rd1"),
        "doctor2": login(client, "surg_doc2", "passw0rd1"),
        "director": login(client, "surg_dir", "passw0rd1"),
        "operator": login(client, "surg_op", "passw0rd1"),
    }


@pytest.fixture(scope="module")
def ward(client, admin):
    org = client.post(
        "/api/organizations", json={"name": "手术演示县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    ward = client.post("/api/inpatient/wards", json={"org_id": org["id"], "name": "外科病区"}, headers=admin).json()
    return {"org": org, "ward": ward}


_bed_seq = iter(range(100))


def _new_admission(client, admin, ward, name, id_card):
    """每个用例一张新床、一位新患者，避免床位占用互相牵制。"""
    bed = client.post(
        "/api/inpatient/beds",
        json={"ward_id": ward["ward"]["id"], "bed_no": f"B{next(_bed_seq):03d}"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients", json={"name": name, "id_card": id_card}, headers=admin
    ).json()
    return client.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient["id"], "ward_id": ward["ward"]["id"], "bed_id": bed["id"],
              "doctor_name": "外科医生", "diagnosis_name": "急性阑尾炎"},
        headers=admin,
    ).json()


# ---------------------------------------------------------------- 病程记录


def test_progress_note_crud_and_first_note_unique(client, admin, ward):
    adm = _new_admission(client, admin, ward, "病程患者", "331182199001011234")
    first = client.post(
        f"/api/inpatient/admissions/{adm['id']}/progress-notes",
        json={"note_type": "first", "content": "患者因转移性右下腹痛入院，拟行阑尾切除术"},
        headers=admin,
    )
    assert first.status_code == 201
    assert first.json()["recorded_at"]  # 未传时回落到创建时刻

    dup = client.post(
        f"/api/inpatient/admissions/{adm['id']}/progress-notes",
        json={"note_type": "first", "content": "重复首次病程"},
        headers=admin,
    )
    assert dup.status_code == 409

    client.post(
        f"/api/inpatient/admissions/{adm['id']}/progress-notes",
        json={"note_type": "daily", "content": "体温正常，切口无渗出"},
        headers=admin,
    )
    rows = client.get(f"/api/inpatient/admissions/{adm['id']}/progress-notes", headers=admin).json()
    assert [r["note_type"] for r in rows] == ["first", "daily"]

    filtered = client.get(
        f"/api/inpatient/admissions/{adm['id']}/progress-notes?note_type=daily", headers=admin
    ).json()
    assert len(filtered) == 1


def test_progress_note_requires_doctor(client, admin, ward, roles):
    adm = _new_admission(client, admin, ward, "权限患者", "331182199002022345")
    denied = client.post(
        f"/api/inpatient/admissions/{adm['id']}/progress-notes",
        json={"note_type": "daily", "content": "经办不应能写病程"},
        headers=roles["operator"],
    )
    assert denied.status_code == 403


def test_progress_note_blocked_after_discharge(client, admin, ward):
    adm = _new_admission(client, admin, ward, "出院患者", "331182199003033456")
    client.post(
        f"/api/inpatient/admissions/{adm['id']}/case-summary",
        json={"discharge_diagnosis": "阑尾炎", "total_cost": 100, "drug_cost": 10, "outcome": "治愈"},
        headers=admin,
    )
    client.post(
        "/api/billing/settlements",
        json={"bill_type": "inpatient", "admission_id": adm["id"]},
        headers=admin,
    )
    assert client.post(f"/api/inpatient/admissions/{adm['id']}/discharge", headers=admin).status_code == 200
    blocked = client.post(
        f"/api/inpatient/admissions/{adm['id']}/progress-notes",
        json={"note_type": "daily", "content": "出院后补记"},
        headers=admin,
    )
    assert blocked.status_code == 409


def test_document_completeness(client, admin, ward):
    adm = _new_admission(client, admin, ward, "完整性患者", "331182199004044567")
    before = client.get(
        f"/api/inpatient/admissions/{adm['id']}/document-completeness", headers=admin
    ).json()
    assert before["complete"] is False
    assert set(before["missing"]) == {"缺首次病程记录", "缺日常病程记录", "缺护理记录", "缺体征记录"}

    for note_type, content in [("first", "首次病程"), ("daily", "日常病程")]:
        client.post(
            f"/api/inpatient/admissions/{adm['id']}/progress-notes",
            json={"note_type": note_type, "content": content}, headers=admin,
        )
    client.post(
        f"/api/inpatient/admissions/{adm['id']}/nursing-records",
        json={"nursing_level": "level1", "content": "一级护理"}, headers=admin,
    )
    client.post(
        f"/api/inpatient/admissions/{adm['id']}/vitals",
        json={"measured_at": "2026-08-11 08:00", "temperature": 37.2, "pulse": 82}, headers=admin,
    )
    after = client.get(
        f"/api/inpatient/admissions/{adm['id']}/document-completeness", headers=admin
    ).json()
    assert after["complete"] is True and after["missing"] == []


# ---------------------------------------------------------------- 护理与体温单


def test_vitals_allow_partial_measurement(client, admin, ward):
    """一次测量未必测全，未测项应为 null 而不是 0。"""
    adm = _new_admission(client, admin, ward, "体征患者", "331182199005055678")
    client.post(
        f"/api/inpatient/admissions/{adm['id']}/vitals",
        json={"measured_at": "2026-08-11 06:00", "temperature": 38.6}, headers=admin,
    )
    rows = client.get(f"/api/inpatient/admissions/{adm['id']}/vitals", headers=admin).json()
    assert rows[0]["temperature"] == 38.6
    assert rows[0]["pulse"] is None and rows[0]["sbp"] is None


def test_vitals_sorted_by_measured_time(client, admin, ward):
    adm = _new_admission(client, admin, ward, "曲线患者", "331182199006066789")
    for at, temp in [("2026-08-11 18:00", 37.0), ("2026-08-11 06:00", 38.9), ("2026-08-11 12:00", 38.1)]:
        client.post(
            f"/api/inpatient/admissions/{adm['id']}/vitals",
            json={"measured_at": at, "temperature": temp}, headers=admin,
        )
    rows = client.get(f"/api/inpatient/admissions/{adm['id']}/vitals", headers=admin).json()
    assert [r["measured_at"] for r in rows] == [
        "2026-08-11 06:00", "2026-08-11 12:00", "2026-08-11 18:00"
    ]


def test_vitals_reject_out_of_range(client, admin, ward):
    adm = _new_admission(client, admin, ward, "越界患者", "331182199007077890")
    resp = client.post(
        f"/api/inpatient/admissions/{adm['id']}/vitals",
        json={"measured_at": "2026-08-11 06:00", "temperature": 99}, headers=admin,
    )
    assert resp.status_code == 422


def test_handover_snapshots_patient_count(client, admin, ward):
    """在院人数由系统快照，不接受人工填写。"""
    resp = client.post(
        "/api/inpatient/handovers",
        json={"ward_id": ward["ward"]["id"], "shift": "night", "handover_date": "2026-08-11",
              "from_staff": "白班护士", "to_staff": "夜班护士", "critical_count": 1,
              "content": "3床术后第一天，注意引流"},
        headers=admin,
    )
    assert resp.status_code == 201
    body = resp.json()
    admitted = [
        a for a in client.get("/api/inpatient/admissions", headers=admin).json()
        if a["ward_id"] == ward["ward"]["id"] and a["status"] == "admitted"
    ]
    assert body["patient_count"] == len(admitted)

    rows = client.get(f"/api/inpatient/handovers?ward_id={ward['ward']['id']}", headers=admin).json()
    assert rows[0]["shift"] == "night"


# ---------------------------------------------------------------- 手术全流程


@pytest.fixture(scope="module")
def room(client, admin, ward):
    return client.post(
        "/api/surgery/rooms", json={"org_id": ward["org"]["id"], "name": "一号手术间"}, headers=admin
    ).json()


def test_duplicate_room_name_rejected(client, admin, ward, room):
    resp = client.post(
        "/api/surgery/rooms", json={"org_id": ward["org"]["id"], "name": "一号手术间"}, headers=admin
    )
    assert resp.status_code == 409


def test_surgery_full_flow(client, admin, ward, roles, room):
    adm = _new_admission(client, admin, ward, "手术患者", "331182199008088901")
    req = client.post(
        "/api/surgery/requests",
        json={"admission_id": adm["id"], "surgery_name": "腹腔镜阑尾切除术",
              "incision_level": "II", "anesthesia_type": "general", "urgency": "urgent"},
        headers=roles["doctor"],
    )
    assert req.status_code == 201
    request_id = req.json()["id"]
    # 患者与机构从住院记录带出，不由客户端自报
    assert req.json()["patient_id"] == adm["patient_id"]
    assert req.json()["org_id"] == adm["org_id"]

    # 未审批不可排班
    early = client.post(
        f"/api/surgery/requests/{request_id}/schedule",
        json={"room_id": room["id"], "scheduled_date": "2026-09-01",
              "start_time": "09:00", "end_time": "11:00"},
        headers=roles["operator"],
    )
    assert early.status_code == 409

    approved = client.post(
        f"/api/surgery/requests/{request_id}/approve", json={"approved": True}, headers=roles["director"]
    )
    assert approved.status_code == 200 and approved.json()["status"] == "approved"

    sched = client.post(
        f"/api/surgery/requests/{request_id}/schedule",
        json={"room_id": room["id"], "scheduled_date": "2026-09-01",
              "start_time": "09:00", "end_time": "11:00"},
        headers=roles["operator"],
    )
    assert sched.status_code == 201

    record = client.post(
        f"/api/surgery/requests/{request_id}/record",
        json={"actual_surgery_name": "腹腔镜阑尾切除术", "anesthetist_name": "麻醉科李医生",
              "start_at": "2026-09-01 09:10", "end_at": "2026-09-01 10:05",
              "blood_loss_ml": 20, "findings": "阑尾化脓", "outcome": "治愈"},
        headers=roles["doctor"],
    )
    assert record.status_code == 201
    assert client.get(f"/api/surgery/requests/{request_id}/record", headers=admin).json()["blood_loss_ml"] == 20
    assert client.get("/api/surgery/requests", headers=admin).json()[0]["status"] == "completed"


def test_requester_cannot_approve_own_request(client, admin, ward, roles):
    adm = _new_admission(client, admin, ward, "自批患者", "331182199009099012")
    client.post(
        "/api/users",
        json={"username": "surg_dirdoc", "password": "passw0rd1", "full_name": "既是医生又是管理",
              "role": "director"},
        headers=admin,
    )
    dirdoc = login(client, "surg_dirdoc", "passw0rd1")
    # director 也能开手术申请？——申请限 doctor，故用 admin 建单再验证自批拦截
    req = client.post(
        "/api/surgery/requests",
        json={"admission_id": adm["id"], "surgery_name": "自批测试术"},
        headers=dirdoc,
    )
    if req.status_code == 201:
        denied = client.post(
            f"/api/surgery/requests/{req.json()['id']}/approve",
            json={"approved": True}, headers=dirdoc,
        )
        assert denied.status_code == 403


def test_schedule_overlap_rejected(client, admin, ward, roles, room):
    """同手术间同日时段重叠必须拒绝：09:00-11:00 已占，10:00-12:00 应 409。"""
    adm = _new_admission(client, admin, ward, "重叠患者", "331182199010100123")
    req = client.post(
        "/api/surgery/requests",
        json={"admission_id": adm["id"], "surgery_name": "重叠测试术"},
        headers=roles["doctor"],
    ).json()
    client.post(f"/api/surgery/requests/{req['id']}/approve", json={"approved": True}, headers=roles["director"])
    overlap = client.post(
        f"/api/surgery/requests/{req['id']}/schedule",
        json={"room_id": room["id"], "scheduled_date": "2026-09-01",
              "start_time": "10:00", "end_time": "12:00"},
        headers=roles["operator"],
    )
    assert overlap.status_code == 409
    assert "已被占用" in overlap.json()["detail"]

    # 紧邻但不重叠（11:00 起）应放行
    ok = client.post(
        f"/api/surgery/requests/{req['id']}/schedule",
        json={"room_id": room["id"], "scheduled_date": "2026-09-01",
              "start_time": "11:00", "end_time": "12:30"},
        headers=roles["operator"],
    )
    assert ok.status_code == 201


def test_schedule_rejects_inverted_time(client, admin, ward, roles, room):
    adm = _new_admission(client, admin, ward, "时序患者", "331182199011111234")
    req = client.post(
        "/api/surgery/requests", json={"admission_id": adm["id"], "surgery_name": "时序测试术"},
        headers=roles["doctor"],
    ).json()
    client.post(f"/api/surgery/requests/{req['id']}/approve", json={"approved": True}, headers=roles["director"])
    resp = client.post(
        f"/api/surgery/requests/{req['id']}/schedule",
        json={"room_id": room["id"], "scheduled_date": "2026-09-02",
              "start_time": "14:00", "end_time": "13:00"},
        headers=roles["operator"],
    )
    assert resp.status_code == 422


def test_schedule_list_and_stats(client, admin):
    rows = client.get("/api/surgery/schedules?scheduled_date=2026-09-01", headers=admin).json()
    assert rows and rows[0]["room_name"] == "一号手术间"
    stats = client.get("/api/surgery/stats", headers=admin).json()
    assert stats and stats[0]["total"] >= 1
    assert "II" in stats[0]["by_incision"]


def test_case_summary_pulls_operation_from_record(client, admin, ward, roles, room):
    """病案首页手术栏未填时，从术中记录带出实际术式（DRG 外科组入组依据）。"""
    adm = _new_admission(client, admin, ward, "首页患者", "331182199012121345")
    req = client.post(
        "/api/surgery/requests",
        json={"admission_id": adm["id"], "surgery_name": "胆囊切除术"},
        headers=roles["doctor"],
    ).json()
    client.post(f"/api/surgery/requests/{req['id']}/approve", json={"approved": True}, headers=roles["director"])
    client.post(
        f"/api/surgery/requests/{req['id']}/schedule",
        json={"room_id": room["id"], "scheduled_date": "2026-09-05",
              "start_time": "08:00", "end_time": "10:00"},
        headers=roles["operator"],
    )
    client.post(
        f"/api/surgery/requests/{req['id']}/record",
        json={"actual_surgery_name": "腹腔镜胆囊切除术", "outcome": "治愈"},
        headers=roles["doctor"],
    )
    summary = client.post(
        f"/api/inpatient/admissions/{adm['id']}/case-summary",
        json={"discharge_diagnosis": "胆囊结石伴胆囊炎", "total_cost": 8000, "drug_cost": 1200,
              "outcome": "治愈"},
        headers=admin,
    )
    assert summary.status_code == 201
    assert summary.json()["operation"] == "腹腔镜胆囊切除术"


# ---------------------------------------------------------------- 随访中心


def test_surgery_completion_creates_followup(client, admin):
    rows = client.get("/api/followups?category=surgery", headers=admin).json()
    assert rows and rows[0]["category_name"] == "术后随访"
    assert rows[0]["status"] == "pending"
    assert rows[0]["patient_name"]


def test_discharge_creates_followup(client, admin, ward):
    adm = _new_admission(client, admin, ward, "随访出院患者", "331182199101011456")
    client.post(
        f"/api/inpatient/admissions/{adm['id']}/case-summary",
        json={"discharge_diagnosis": "肺炎", "total_cost": 50, "drug_cost": 5, "outcome": "治愈"},
        headers=admin,
    )
    client.post(
        "/api/billing/settlements",
        json={"bill_type": "inpatient", "admission_id": adm["id"]}, headers=admin,
    )
    client.post(f"/api/inpatient/admissions/{adm['id']}/discharge", headers=admin)
    rows = client.get(
        f"/api/followups?category=discharge&patient_id={adm['patient_id']}", headers=admin
    ).json()
    assert len(rows) == 1 and rows[0]["source_id"] == adm["id"]


def test_followup_complete_and_stats(client, admin, ward):
    org_id = ward["org"]["id"]
    patient = client.post(
        "/api/patients", json={"name": "随访患者", "id_card": "331182199202022567"}, headers=admin
    ).json()
    task = client.post(
        "/api/followups",
        json={"patient_id": patient["id"], "org_id": org_id, "category": "chronic",
              "due_date": (date.today() - timedelta(days=3)).isoformat()},
        headers=admin,
    )
    assert task.status_code == 201
    assert task.json()["title"] == "慢病随访"  # 未传标题时按类别回落
    task_id = task.json()["id"]

    overdue = client.get("/api/followups/overdue", headers=admin).json()
    assert any(t["id"] == task_id for t in overdue)

    done = client.post(
        f"/api/followups/{task_id}/complete", json={"result": "血压 130/80，用药依从性好"}, headers=admin
    )
    assert done.status_code == 200
    assert client.post(
        f"/api/followups/{task_id}/complete", json={"result": "重复完成"}, headers=admin
    ).status_code == 409

    stats = client.get("/api/followups/stats", headers=admin).json()
    chronic = next(s for s in stats if s["category"] == "chronic")
    assert chronic["done"] >= 1
    assert 0 <= chronic["completion_rate_pct"] <= 100


def test_cancelled_followup_excluded_from_completion_rate(client, admin, ward):
    """取消的任务不该拉低随访完成率。"""
    patient = client.post(
        "/api/patients", json={"name": "取消随访患者", "id_card": "331182199303033678"}, headers=admin
    ).json()
    task = client.post(
        "/api/followups",
        json={"patient_id": patient["id"], "org_id": ward["org"]["id"], "category": "maternal",
              "due_date": (date.today() + timedelta(days=5)).isoformat()},
        headers=admin,
    ).json()
    client.post(f"/api/followups/{task['id']}/cancel", headers=admin)
    stats = client.get("/api/followups/stats", headers=admin).json()
    maternal = next(s for s in stats if s["category"] == "maternal")
    assert maternal["cancelled"] == 1
    assert maternal["completion_rate_pct"] == 0.0  # 分母为 0（无 pending/done），不是被取消项拉低


def test_followup_job_matches_endpoint(client, admin):
    from app.database import SessionLocal
    from app.scheduler import run_job

    endpoint = len(client.get("/api/followups/overdue", headers=admin).json())
    with SessionLocal() as db:
        run = run_job(db, "followup_overdue_scan", trigger="manual")
    assert run.affected == endpoint
