"""慢专病患者移动端（`/api/portal/spd`）：居民令牌边界、自查申请、居家监测、任务闭环。

重点验证两条边界：
1. 居民令牌不能进业务接口，业务令牌也不能进居民接口——两边都测；
2. 居民只能看自己和代管家人的档案，`patient_id` 传别人的一律 403。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def h(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def base(client, h):
    org = client.post(
        "/api/organizations",
        json={"name": "居民端慢专病卫生院", "org_type": "township", "level": "township"},
        headers=h,
    ).json()
    me = client.post(
        "/api/patients",
        json={"name": "居民甲", "id_card": "330199198501010011", "gender": "男",
              "birth_date": "1985-01-01", "phone": "13800001111"},
        headers=h,
    ).json()
    other = client.post(
        "/api/patients",
        json={"name": "居民乙", "id_card": "330199198501010022", "gender": "女",
              "birth_date": "1986-01-01", "phone": "13800002222"},
        headers=h,
    ).json()
    return {"org": org, "me": me, "other": other, "phone": "13800001111"}


@pytest.fixture(scope="module")
def ph(client, base):
    """居民令牌：短信验证码登录 + 实名绑定。"""
    phone = base["phone"]
    # console 通道且非生产环境时验证码在响应里回显，与既有居民端用例同一取法
    code = client.post(
        "/api/portal/auth/sms/code", json={"phone": phone}
    ).json()["debug_code"]
    login = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    client.post(
        "/api/portal/auth/realname",
        json={"name": base["me"]["name"], "id_card": base["me"]["id_card"]},
        headers=headers,
    )
    return headers


def test_business_token_rejected_on_portal(client, h):
    resp = client.get("/api/portal/spd/home", headers=h)
    assert resp.status_code == 401, "业务端令牌不得进入居民端接口"


def test_portal_token_rejected_on_business(client, ph):
    resp = client.get("/api/spd/enrollments", headers=ph)
    assert resp.status_code == 401, "居民令牌不得进入业务接口"


def test_home_before_enrollment(client, ph):
    home = client.get("/api/portal/spd/home", headers=ph).json()
    assert home["enrolled"] is False
    assert home["todo"]["tasks"] == 0


def test_self_screening_and_service_apply(client, ph):
    scales = client.get("/api/portal/spd/scales", headers=ph).json()
    assert scales, "应有可自查的筛查量表"
    scale = next(s for s in scales if s["code"] == "scr_hypertension")

    draft = client.post(
        "/api/portal/spd/screenings",
        json={"program_code": "hypertension", "scale_code": scale["code"], "draft": True,
              "answers": {"family": "是", "salt": "是", "overweight": "是",
                          "smoke": "是", "drink": "是", "symptom": "是"}},
        headers=ph,
    ).json()
    assert draft["draft"] is True, "草稿保存不落库"

    resp = client.post(
        "/api/portal/spd/screenings",
        json={"program_code": "hypertension", "scale_code": scale["code"],
              "answers": {"family": "是", "salt": "是", "overweight": "是",
                          "smoke": "是", "drink": "是", "symptom": "是"}},
        headers=ph,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["risk_level"] == "high"
    assert body["can_apply"] is True

    apply = client.post(
        "/api/portal/spd/service-applies",
        json={"program_code": "hypertension", "screening_id": body["id"],
              "note": "希望纳入管理"},
        headers=ph,
    )
    assert apply.status_code == 201
    again = client.post(
        "/api/portal/spd/service-applies",
        json={"program_code": "hypertension"}, headers=ph,
    )
    assert again.status_code == 409, "重复提交同病种申请应被拒"


def test_self_screening_does_not_auto_enroll(client, h):
    """居民自填问卷不足以纳管，只落一条待复核的筛查记录。"""
    screenings = client.get("/api/spd/screenings?source=self", headers=h).json()
    assert screenings
    assert all(not s["reviewed"] for s in screenings)
    enrollments = client.get("/api/spd/enrollments", headers=h).json()
    assert not enrollments, "自查不应直接产生纳管档案"


def test_staff_accepts_apply_puts_into_pool(client, h):
    applies = client.get("/api/spd/service-applies?status=pending", headers=h).json()
    assert applies
    resp = client.post(
        f"/api/spd/service-applies/{applies[0]['id']}/handle",
        json={"status": "accepted", "handle_note": "已受理"}, headers=h,
    )
    assert resp.status_code == 200
    candidates = client.get("/api/spd/candidates?status=target", headers=h).json()
    assert candidates, "受理后应进入目标池等待签约"


def test_home_measurement_records_and_levels(client, ph, h, base):
    """居民端录入的居家指标要按管理目标判等级——这决定居民看到的红黄绿。"""
    client.post(
        "/api/spd/enrollments",
        json={"patient_id": base["me"]["id"], "program_code": "hypertension",
              "org_id": base["org"]["id"], "risk_level": "mid"},
        headers=h,
    )
    high = client.post(
        "/api/portal/spd/measurements",
        json={"metric": "bp_sys", "value": 172, "unit": "mmHg",
              "program_code": "hypertension"},
        headers=ph,
    )
    assert high.status_code == 201, high.text
    assert high.json()["level"] == "high"

    normal = client.post(
        "/api/portal/spd/measurements",
        json={"metric": "bp_sys", "value": 126, "unit": "mmHg",
              "program_code": "hypertension"},
        headers=ph,
    ).json()
    assert normal["level"] == "normal"

    rows = client.get("/api/portal/spd/measurements?metric=bp_sys", headers=ph).json()
    assert len(rows) == 2

    home = client.get("/api/portal/spd/home", headers=ph).json()
    assert home["enrolled"] is True
    assert home["latest_metrics"]["bp_sys"]["value"] == 126


def test_cross_patient_access_denied(client, ph, base):
    resp = client.get(
        f"/api/portal/spd/home?patient_id={base['other']['id']}", headers=ph
    )
    assert resp.status_code == 403, "不能查看未代管的他人档案"


def test_task_submit_goes_to_review_not_done(client, ph, h, base):
    """居民提交只到"待审核"，不是"已完成"——否则完成率变成"居民点了几次提交"。"""
    task = client.post(
        "/api/spd/tasks",
        json={"patient_id": base["me"]["id"], "title": "居家血压填报",
              "task_type": "followup", "program_code": "hypertension"},
        headers=h,
    ).json()
    resp = client.post(
        f"/api/portal/spd/tasks/{task['id']}/submit",
        json={"result": {"bp_sys": 130}}, headers=ph,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "submitted"


def test_task_requiring_evidence_blocks_submit(client, ph, h, base):
    """居民经自己的上传通道传凭证；上传与校验走平台附件同一份实现。"""
    task = client.post(
        "/api/spd/tasks",
        json={"patient_id": base["me"]["id"], "title": "上传检查报告",
              "task_type": "report", "require_evidence": True},
        headers=h,
    ).json()
    resp = client.post(
        f"/api/portal/spd/tasks/{task['id']}/submit", json={"result": {}}, headers=ph
    )
    assert resp.status_code == 422

    bad_type = client.post(
        f"/api/portal/spd/tasks/{task['id']}/attachments",
        files={"file": ("report.exe", b"MZ...", "application/octet-stream")},
        headers=ph,
    )
    assert bad_type.status_code == 415, "白名单与平台上传是同一份"

    upload = client.post(
        f"/api/portal/spd/tasks/{task['id']}/attachments",
        files={"file": ("report.jpg", b"\xff\xd8fake-jpeg", "image/jpeg")},
        headers=ph,
    )
    assert upload.status_code == 201, upload.text
    attachment_id = upload.json()["attachment_id"]
    ok = client.post(
        f"/api/portal/spd/tasks/{task['id']}/submit",
        json={"result": {}, "evidence": [attachment_id]}, headers=ph,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "submitted"


def test_journey_and_referral_visible(client, ph, h, base):
    client.post(
        "/api/spd/referrals",
        json={"patient_id": base["me"]["id"], "program_code": "hypertension",
              "reason": "血压不达标"},
        headers=h,
    )
    journey = client.get("/api/portal/spd/journey", headers=ph).json()
    assert journey["programs"]
    assert journey["programs"][0]["referrals"]

    referrals = client.get("/api/portal/spd/referrals", headers=ph).json()
    assert referrals
    detail = client.get(f"/api/portal/spd/referrals/{referrals[0]['id']}", headers=ph).json()
    assert detail["steps"][0]["step"] == "发起"


def test_intervention_feedback_loop(client, ph, h, base):
    client.post(
        "/api/spd/interventions",
        json={"patient_ids": [base["me"]["id"]], "program_code": "hypertension",
              "goal": "限盐", "content": "每日食盐不超过5克"},
        headers=h,
    )
    items = client.get("/api/portal/spd/interventions", headers=ph).json()
    assert items and items[0]["read"] is False
    resp = client.post(
        f"/api/portal/spd/interventions/{items[0]['id']}/feedback",
        json={"feedback": "已按要求执行", "done": True}, headers=ph,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"


def test_education_push_and_read(client, ph, h, base):
    material = client.post(
        "/api/spd/edu-materials",
        json={"code": "edu_salt", "title": "限盐指导", "program_code": "hypertension",
              "content": "每日食盐不超过5克"},
        headers=h,
    ).json()
    client.post(
        "/api/spd/edu-pushes",
        json={"material_id": material["id"], "patient_ids": [base["me"]["id"]],
              "channel": "app"},
        headers=h,
    )
    items = client.get("/api/portal/spd/edu", headers=ph).json()
    assert items and items[0]["title"] == "限盐指导"
    read = client.post(f"/api/portal/spd/edu/{items[0]['id']}/read", headers=ph)
    assert read.json()["status"] == "read"
    stats = client.get("/api/spd/edu-pushes/stats", headers=h).json()
    assert stats["read"] >= 1


def test_self_followup_answer(client, ph, h, base):
    rules = client.get("/api/spd/followup-rules?scene=outpatient", headers=h).json()
    rule = next(r for r in rules if r["code"] == "fr_chronic")
    client.post(
        "/api/spd/followup-plans",
        json={"patient_id": base["me"]["id"], "rule_id": rule["id"],
              "org_id": base["org"]["id"]},
        headers=h,
    )
    records = client.get("/api/portal/spd/followups", headers=ph).json()
    assert records
    resp = client.post(
        f"/api/portal/spd/followups/{records[0]['id']}/self-answer",
        json={"answers": {"bp_sys": 190, "adherence": "经常漏服"}}, headers=ph,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["abnormal_level"] == "high", "自助随访的异常分级与医护执行一致"


def test_consult_thread(client, ph, h, base):
    started = client.post(
        "/api/portal/spd/consults",
        json={"program_code": "hypertension", "content": "最近头晕怎么办"},
        headers=ph,
    )
    assert started.status_code == 201
    consult_id = started.json()["consult_id"]
    again = client.post(
        "/api/portal/spd/consults",
        json={"program_code": "hypertension", "content": "补充：昨天量了180"},
        headers=ph,
    ).json()
    assert again["consult_id"] == consult_id, "同病种开放会话应复用"

    reply = client.post(
        f"/api/spd/consults/{consult_id}/reply",
        json={"content": "请尽快到卫生院复测血压"}, headers=h,
    )
    assert reply.status_code == 200
    messages = client.get(
        f"/api/portal/spd/consults/{consult_id}/messages", headers=ph
    ).json()
    assert len(messages) == 3
    assert messages[-1]["sender"] == "doctor"
