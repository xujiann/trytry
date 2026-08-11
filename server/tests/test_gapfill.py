"""块4：细目补齐——中药制剂/消毒成本/课件与实训/产前筛查/绩效整改/上门服务。"""
import io
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def env(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "补齐县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    users = {}
    for username, role in [
        ("gf_doc", "doctor"),
        ("gf_pharm", "pharmacist"),
        ("gf_op", "operator"),
        ("gf_dir", "director"),
        ("gf_ph", "public_health"),
    ]:
        resp = client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
        assert resp.status_code in (200, 201), resp.text
        users[role] = login(client, username, "pass123456")
    patient = client.post(
        "/api/patients",
        json={"name": "补齐患者", "id_card": "330281199205057012", "phone": "13900001111"},
        headers=admin,
    ).json()
    return {"org": org, "users": users, "patient": patient}


# ---------------- ⑭ 中药制剂管理 ----------------


def test_tcm_formula_and_batch_expiry(client, admin, env):
    pharmacist = env["users"]["pharmacist"]
    formula = client.post(
        "/api/tcm/formulas",
        json={
            "code": "ZJ001",
            "name": "院内清热合剂",
            "dosage_form": "decoction",
            "composition": "金银花15g 连翘12g 甘草6g",
            "process": "水煎两次合并滤过分装",
            "indication": "外感风热",
            "shelf_life_months": 6,
        },
        headers=pharmacist,
    )
    assert formula.status_code == 201, formula.text
    formula_id = formula.json()["id"]
    assert formula.json()["dosage_form_name"] == "合剂/汤剂"
    # 编码唯一
    assert client.post(
        "/api/tcm/formulas",
        json={"code": "ZJ001", "name": "重复编码"},
        headers=pharmacist,
    ).status_code == 409
    # 效期缺省按配方有效期推算（6 个月 ≈ 180 天）
    batch = client.post(
        "/api/tcm/preparation-batches",
        json={
            "formula_id": formula_id,
            "batch_no": "P20260501",
            "org_id": env["org"]["id"],
            "quantity": 100,
            "produced_date": "2026-05-01",
        },
        headers=pharmacist,
    )
    assert batch.status_code == 201, batch.text
    assert batch.json()["expire_date"] == (date(2026, 5, 1) + timedelta(days=180)).isoformat()
    # 已过期批次
    expired = client.post(
        "/api/tcm/preparation-batches",
        json={
            "formula_id": formula_id,
            "batch_no": "P20250101",
            "org_id": env["org"]["id"],
            "quantity": 20,
            "produced_date": "2025-01-01",
            "expire_date": "2025-06-30",
        },
        headers=pharmacist,
    ).json()
    assert expired["expired"] is True
    listed = client.get("/api/tcm/preparation-batches", headers=admin).json()
    assert {b["batch_no"] for b in listed} == {"P20260501", "P20250101"}
    expiring = client.get(
        "/api/tcm/preparation-batches/expiring?days=30&today=2026-10-20", headers=admin
    ).json()
    assert {b["batch_no"] for b in expiring} == {"P20250101", "P20260501"}
    # 过期批次禁止发放，未过期可发放
    assert client.post(
        f"/api/tcm/preparation-batches/{expired['id']}/release", headers=pharmacist
    ).status_code == 409
    released = client.post(
        f"/api/tcm/preparation-batches/{batch.json()['id']}/release?today=2026-06-01",
        headers=pharmacist,
    )
    assert released.status_code == 200 and released.json()["status"] == "released"
    # 越权与不存在
    assert client.post(
        "/api/tcm/formulas", json={"code": "ZJ900", "name": "越权"}, headers=env["users"]["operator"]
    ).status_code == 403
    assert client.post(
        "/api/tcm/preparation-batches",
        json={
            "formula_id": 9999,
            "batch_no": "PX",
            "org_id": env["org"]["id"],
            "quantity": 1,
            "produced_date": "2026-01-01",
        },
        headers=pharmacist,
    ).status_code == 404


# ---------------- ⑥ 消毒供应成本核算 ----------------


def test_cssd_cost_accounting(client, admin, env):
    operator = env["users"]["operator"]
    batch = client.post(
        "/api/cssd/batches",
        json={"batch_no": "GF-CSSD-01", "center_org_id": env["org"]["id"], "item_name": "手术器械包", "quantity": 20},
        headers=operator,
    )
    assert batch.status_code == 201, batch.text
    batch_id = batch.json()["id"]
    for cost_type, amount in [("labor", 120.0), ("material", 60.5), ("energy", 19.5)]:
        resp = client.post(
            "/api/cssd/cost-items",
            json={"batch_id": batch_id, "cost_type": cost_type, "amount": amount},
            headers=operator,
        )
        assert resp.status_code == 201, resp.text
    stats = client.get(f"/api/cssd/cost-stats?batch_id={batch_id}", headers=admin).json()
    row = stats["batches"][0]
    assert row["total_cost"] == 200.0
    assert row["unit_cost"] == 10.0  # 200 / 20 件
    assert stats["overall_unit_cost"] == 10.0
    assert stats["by_cost_type"]["labor"]["amount"] == 120.0
    assert stats["by_cost_type"]["labor"]["name"] == "人工"
    assert len(client.get(f"/api/cssd/cost-items?batch_id={batch_id}", headers=admin).json()) == 3
    assert client.post(
        "/api/cssd/cost-items",
        json={"batch_id": 99999, "cost_type": "labor", "amount": 1},
        headers=operator,
    ).status_code == 404
    assert client.post(
        "/api/cssd/cost-items",
        json={"batch_id": batch_id, "cost_type": "labor", "amount": 0},
        headers=operator,
    ).status_code == 422  # 金额须大于 0


# ---------------- ⑳ 课件资源管理 ----------------


def test_course_materials_attachment_and_play_count(client, admin, env):
    director = env["users"]["director"]
    course = client.post(
        "/api/education/courses",
        json={"title": "县域适宜技术课程", "course_type": "vod", "category": "tcm"},
        headers=admin,  # 课程目录由管理员维护，课件资源则由业务角色补充
    )
    assert course.status_code == 201, course.text
    course_id = course.json()["id"]
    material = client.post(
        f"/api/education/courses/{course_id}/materials",
        json={"title": "针灸操作要点", "material_type": "slide"},
        headers=director,
    )
    assert material.status_code == 201, material.text
    material_id = material.json()["id"]
    # 挂附件（owner_type=course_material）
    upload = client.post(
        "/api/attachments",
        files={"file": ("slide.pdf", io.BytesIO(b"%PDF-1.4 course"), "application/pdf")},
        data={"owner_type": "course_material", "owner_id": str(material_id)},
        headers=director,
    )
    assert upload.status_code == 201, upload.text
    listed = client.get(f"/api/education/courses/{course_id}/materials", headers=admin).json()
    assert listed[0]["attachments"] == 1
    assert listed[0]["material_type_name"] == "课件"
    # 点播计数
    for expected in (1, 2, 3):
        played = client.post(f"/api/education/materials/{material_id}/play", headers=admin)
        assert played.status_code == 200 and played.json()["play_count"] == expected
    stats = client.get("/api/education/material-stats", headers=admin).json()
    assert stats["total_plays"] == 3 and stats["top"][0]["id"] == material_id
    assert client.post(
        "/api/education/courses/9999/materials", json={"title": "无课程"}, headers=director
    ).status_code == 404
    assert client.post("/api/education/materials/9999/play", headers=admin).status_code == 404


# ---------------- ㉑ 适宜技术实训管理 ----------------


def test_training_plan_enroll_and_assessment(client, admin, env):
    director, doctor, operator = (
        env["users"]["director"],
        env["users"]["doctor"],
        env["users"]["operator"],
    )
    technique = client.post(
        "/api/tcm/techniques",
        json={"name": "耳穴压豆", "category": "中医外治", "indication": "失眠"},
        headers=admin,
    ).json()
    plan = client.post(
        "/api/education/training-plans",
        json={
            "title": "耳穴压豆实训班",
            "org_id": env["org"]["id"],
            "technique_id": technique["id"],
            "plan_date": "2026-09-01",
            "capacity": 2,
            "trainer": "王主任",
        },
        headers=director,
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    assert plan.json()["remaining"] == 2
    assert client.post(f"/api/education/training-plans/{plan_id}/enroll", headers=doctor).status_code == 201
    assert client.post(f"/api/education/training-plans/{plan_id}/enroll", headers=doctor).status_code == 409  # 重复报名
    assert client.post(f"/api/education/training-plans/{plan_id}/enroll", headers=operator).status_code == 201
    # 名额已满
    assert client.post(
        f"/api/education/training-plans/{plan_id}/enroll", headers=env["users"]["public_health"]
    ).status_code == 409
    plans = {p["id"]: p for p in client.get("/api/education/training-plans", headers=admin).json()}
    assert plans[plan_id]["enrolled"] == 2 and plans[plan_id]["remaining"] == 0
    enrollments = client.get(
        f"/api/education/training-plans/{plan_id}/enrollments", headers=admin
    ).json()
    doctor_id = next(e["user_id"] for e in enrollments if e["username"] == "gf_doc")
    operator_id = next(e["user_id"] for e in enrollments if e["username"] == "gf_op")
    # 考核：须已报名，60 分及格
    assert client.post(
        f"/api/education/training-plans/{plan_id}/assessments",
        json={"user_id": doctor_id, "score": 88, "comment": "操作规范"},
        headers=director,
    ).json()["passed"] is True
    assert client.post(
        f"/api/education/training-plans/{plan_id}/assessments",
        json={"user_id": operator_id, "score": 50},
        headers=director,
    ).json()["passed"] is False
    # 取消报名后不可录考核
    ph = env["users"]["public_health"]
    assert client.post(
        f"/api/education/training-plans/{plan_id}/assessments",
        json={"user_id": 99999, "score": 90},
        headers=director,
    ).status_code == 409
    stats = client.get(f"/api/education/training-plans/{plan_id}/assessments", headers=admin).json()
    assert stats["total"] == 2 and stats["passed"] == 1 and stats["pass_rate_pct"] == 50.0
    # 退报后名额释放
    assert client.post(f"/api/education/training-plans/{plan_id}/cancel-enroll", headers=operator).status_code == 200
    assert client.post(f"/api/education/training-plans/{plan_id}/enroll", headers=ph).status_code == 201
    assert client.post(
        "/api/education/training-plans",
        json={"title": "越权发布", "org_id": env["org"]["id"], "plan_date": "2026-09-01"},
        headers=operator,
    ).status_code == 403


# ---------------- ㉔ 产前筛查与诊断 ----------------


def test_prenatal_screening_flags_high_risk(client, admin, env):
    doctor = env["users"]["doctor"]
    record = client.post(
        "/api/maternal/records",
        json={"patient_id": env["patient"]["id"], "lmp": "2026-01-01", "gravidity": 1, "parity": 0},
        headers=doctor,
    )
    assert record.status_code == 201, record.text
    record_id = record.json()["id"]
    assert record.json()["high_risk"] is False
    low = client.post(
        "/api/maternal/screenings",
        json={
            "record_id": record_id,
            "screen_type": "down",
            "screen_date": "2026-04-01",
            "gest_week": 16,
            "result": "low_risk",
            "indicator": "21三体风险 1/2000",
        },
        headers=doctor,
    )
    assert low.status_code == 201 and low.json()["flagged_high_risk"] is False
    records = client.get("/api/maternal/records", headers=admin).json()
    assert next(r for r in records if r["id"] == record_id)["high_risk"] is False  # 低风险不误标
    high = client.post(
        "/api/maternal/screenings",
        json={
            "record_id": record_id,
            "screen_type": "nipt",
            "screen_date": "2026-05-01",
            "gest_week": 20,
            "result": "high_risk",
            "indicator": "21三体高风险",
            "conclusion": "建议羊水穿刺产前诊断",
        },
        headers=doctor,
    )
    assert high.status_code == 201 and high.json()["flagged_high_risk"] is True
    updated = next(
        r for r in client.get("/api/maternal/records", headers=admin).json() if r["id"] == record_id
    )
    assert updated["high_risk"] is True  # 高危自动标记
    assert "无创产前基因检测高风险" in updated["risk_factors"]
    stats = client.get("/api/maternal/screening-stats", headers=admin).json()
    assert stats["total"] == 2 and stats["high_risk_detect_rate_pct"] == 50.0
    assert stats["by_type"]["nipt"]["name"] == "无创产前基因检测"
    assert len(client.get(f"/api/maternal/screenings?record_id={record_id}", headers=admin).json()) == 2
    assert client.post(
        "/api/maternal/screenings",
        json={"record_id": 9999, "screen_type": "down", "screen_date": "2026-04-01"},
        headers=doctor,
    ).status_code == 404
    assert client.post(
        "/api/maternal/screenings",
        json={"record_id": record_id, "screen_type": "down", "screen_date": "2026-04-01"},
        headers=env["users"]["operator"],
    ).status_code == 403


# ---------------- ㉟ 绩效自评改进 ----------------


def test_improvement_task_closed_loop(client, admin, env):
    director, operator = env["users"]["director"], env["users"]["operator"]
    task = client.post(
        "/api/performance/improvements",
        json={
            "org_id": env["org"]["id"],
            "indicator_key": "referral_completion",
            "problem": "下转结案率低于考核目标",
            "owner_name": "张主任",
            "due_date": "2026-01-31",
        },
        headers=director,
    )
    assert task.status_code == 201, task.text
    task_id = task.json()["id"]
    assert task.json()["status"] == "open"
    # 期限已过 → 超期标记
    overdue = client.get(
        f"/api/performance/improvements?overdue_only=true&today=2026-03-01", headers=admin
    ).json()
    assert task_id in {t["id"] for t in overdue}
    assert overdue[0]["overdue"] is True
    # 整改中 → 提交完成（须填结果说明）→ 确认
    assert client.post(
        f"/api/performance/improvements/{task_id}/progress",
        json={"measures": "每周调度下转病例"},
        headers=operator,
    ).json()["status"] == "in_progress"
    assert client.post(
        f"/api/performance/improvements/{task_id}/progress",
        json={"complete": True},
        headers=operator,
    ).status_code == 422
    assert client.post(
        f"/api/performance/improvements/{task_id}/progress",
        json={"complete": True, "completion_note": "结案率已提升至 92%"},
        headers=operator,
    ).json()["status"] == "completed"
    # 确认不通过 → 退回整改中
    assert client.post(
        f"/api/performance/improvements/{task_id}/verify",
        json={"approve": False, "comment": "佐证材料不足"},
        headers=director,
    ).json()["status"] == "in_progress"
    client.post(
        f"/api/performance/improvements/{task_id}/progress",
        json={"complete": True, "completion_note": "补充台账与病例清单"},
        headers=operator,
    )
    verified = client.post(
        f"/api/performance/improvements/{task_id}/verify",
        json={"approve": True, "comment": "整改到位"},
        headers=director,
    ).json()
    assert verified["status"] == "verified" and verified["verified_by"]
    assert verified["overdue"] is False  # 已关闭不再计超期
    assert client.post(
        f"/api/performance/improvements/{task_id}/verify", json={"approve": True}, headers=director
    ).status_code == 409
    stats = client.get("/api/performance/improvement-stats?today=2026-03-01", headers=admin).json()
    assert stats["total"] == 1 and stats["by_status"]["verified"]["count"] == 1
    assert stats["closed_rate_pct"] == 100.0 and stats["overdue"] == 0
    assert client.post(
        "/api/performance/improvements",
        json={
            "org_id": env["org"]["id"],
            "problem": "越权下达",
            "owner_name": "某某",
            "due_date": "2026-06-01",
        },
        headers=operator,
    ).status_code == 403


# ---------------- ⑨ 上门服务调度 ----------------


def test_home_visit_dispatch_flow_and_contract_link(client, admin, env):
    doctor, operator = env["users"]["doctor"], env["users"]["operator"]
    contract = client.post(
        "/api/contracts",
        json={
            "patient_id": env["patient"]["id"],
            "org_id": env["org"]["id"],
            "doctor_name": "李医生",
            "package": "premium",
            "signed_date": "2026-01-05",
        },
        headers=doctor,
    )
    assert contract.status_code == 201, contract.text
    # 未传 contract_id 时按患者+机构自动关联履约中签约
    order = client.post(
        "/api/homevisits",
        json={
            "patient_id": env["patient"]["id"],
            "org_id": env["org"]["id"],
            "service_type": "nursing",
            "demand": "留置导尿管更换",
            "address": "某某村 12 号",
            "expect_date": "2026-06-10",
        },
        headers=operator,
    )
    assert order.status_code == 201, order.text
    order_id = order.json()["id"]
    assert order.json()["contract_id"] == contract.json()["id"]
    assert order.json()["status"] == "applied"
    assert order.json()["service_type_name"] == "上门护理"
    # 未派单不可完成
    assert client.post(
        f"/api/homevisits/{order_id}/complete", json={"service_note": "已完成"}, headers=operator
    ).status_code == 409
    dispatched = client.post(
        f"/api/homevisits/{order_id}/dispatch", json={"assignee_name": "赵护士"}, headers=operator
    ).json()
    assert dispatched["status"] == "dispatched" and dispatched["dispatched_at"]
    assert client.post(
        f"/api/homevisits/{order_id}/dispatch", json={"assignee_name": "重复派单"}, headers=operator
    ).status_code == 409
    completed = client.post(
        f"/api/homevisits/{order_id}/complete",
        json={"service_note": "已更换导尿管，指导家属护理"},
        headers=operator,
    ).json()
    assert completed["status"] == "completed" and completed["completed_at"]
    assert client.post(f"/api/homevisits/{order_id}/cancel", headers=operator).status_code == 409
    # 无签约患者的工单：签约关联为空
    other = client.post(
        "/api/patients",
        json={"name": "无签约患者", "id_card": "330281199205057013"},
        headers=admin,
    ).json()
    free = client.post(
        "/api/homevisits",
        json={"patient_id": other["id"], "org_id": env["org"]["id"], "service_type": "doctor"},
        headers=operator,
    ).json()
    assert free["contract_id"] is None
    assert client.post(f"/api/homevisits/{free['id']}/cancel", headers=operator).json()["status"] == "cancelled"
    stats = client.get("/api/homevisits/stats", headers=admin).json()
    assert stats["total"] == 2 and stats["contract_linked"] == 1
    assert stats["contract_linked_ratio_pct"] == 50.0
    assert stats["by_status"]["completed"] == 1
    # 校验：签约与患者不一致 / 签约不存在 / 越权
    assert client.post(
        "/api/homevisits",
        json={
            "patient_id": other["id"],
            "org_id": env["org"]["id"],
            "service_type": "nursing",
            "contract_id": contract.json()["id"],
        },
        headers=operator,
    ).status_code == 422
    assert client.post(
        "/api/homevisits",
        json={
            "patient_id": env["patient"]["id"],
            "org_id": env["org"]["id"],
            "service_type": "nursing",
            "contract_id": 9999,
        },
        headers=operator,
    ).status_code == 404
    assert client.get("/api/homevisits", headers=admin).status_code == 200
    assert client.post(
        "/api/homevisits",
        json={"patient_id": env["patient"]["id"], "org_id": env["org"]["id"], "service_type": "nursing"},
        headers=env["users"]["pharmacist"],
    ).status_code == 403


def test_gapfill_endpoints_require_login(client):
    for path in [
        "/api/tcm/formulas",
        "/api/cssd/cost-stats",
        "/api/education/material-stats",
        "/api/education/training-plans",
        "/api/maternal/screenings",
        "/api/performance/improvements",
        "/api/homevisits",
    ]:
        assert client.get(path).status_code == 401, path
