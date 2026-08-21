"""第四阶段 M1 中危修复回归：M-1~M-6 与顺手清理 L-6/L-7/L-10。"""

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app
from app.routers.portal import _reset_portal_failures


@pytest.fixture(scope="module")
def client():
    reset_database()
    _reset_portal_failures()
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def headers(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def admin(client):
    return headers(login(client, "admin", "admin123"))


@pytest.fixture(scope="module")
def setup(client, admin):
    org1 = client.post(
        "/api/organizations",
        json={"name": "M1县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    org2 = client.post(
        "/api/organizations",
        json={"name": "M1卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "M1患者", "id_card": "320981199404041234", "phone": "13800001234"},
        headers=admin,
    ).json()
    users = {}
    for username, role, org in [
        ("m1_doc1", "doctor", org1["id"]),
        ("m1_doc2", "doctor", org2["id"]),
        ("m1_op", "operator", org1["id"]),
        ("m1_pharm", "pharmacist", org1["id"]),
    ]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org},
            headers=admin,
        )
        users[username] = login(client, username, "pass123456")
    return {"org1": org1, "org2": org2, "patient": patient, "tokens": users}


def _make_critical_report(client, admin, setup, from_org_id, item_code="M1K"):
    req = client.post(
        "/api/exams",
        json={
            "patient_id": setup["patient"]["id"],
            "from_org_id": from_org_id,
            "center_type": "lab",
            "item_code": item_code,
            "item_name": "血钾",
        },
        headers=headers(setup["tokens"]["m1_doc1"]),
    ).json()
    report = client.post(
        f"/api/exams/{req['id']}/report",
        json={"conclusion": "血钾 7.0 危急", "critical": True, "reported_by": "检验科"},
        headers=headers(setup["tokens"]["m1_doc1"]),
    ).json()
    return req, report


# ---------------------------------------------------------------- M-1 存量危急值


def test_legacy_critical_report_enters_loop(client, admin, setup):
    """迁移前存量危急报告（critical_status=''）：可催办、可确认、可处置。"""
    req, report = _make_critical_report(client, admin, setup, setup["org1"]["id"], "LEGACY")
    # 模拟存量数据：直接把 critical_status 置回空串（迁移前形态）
    from app.database import SessionLocal
    from app.models import ExamReport

    db = SessionLocal()
    try:
        row = db.get(ExamReport, report["id"])
        row.critical_status = ""
        db.commit()
    finally:
        db.close()

    # 出现在超时未确认催办清单（today 晚于报告日期 → 全部计入）
    unacked = client.get(
        "/api/exams/critical/unacknowledged?today=2099-01-01", headers=admin
    ).json()
    assert any(r["report_id"] == report["id"] for r in unacked)

    # 可正常确认接收 → 处置，闭环不再排除存量数据
    ack = client.post(
        f"/api/exams/reports/{report['id']}/acknowledge",
        headers=headers(setup["tokens"]["m1_doc1"]),
    )
    assert ack.status_code == 200
    assert ack.json()["critical_status"] == "acknowledged"
    res = client.post(
        f"/api/exams/reports/{report['id']}/resolve",
        json={"note": "已复查处置"},
        headers=headers(setup["tokens"]["m1_doc1"]),
    )
    assert res.status_code == 200
    assert res.json()["critical_status"] == "resolved"


def test_migration_backfills_legacy_critical_status(client):
    """迁移回填语句：critical=True 且状态空串 → notified（SQL 语义验证）。"""
    import sqlalchemy as sa

    from app.database import engine

    exam_reports = sa.table(
        "exam_reports", sa.column("critical", sa.Boolean), sa.column("critical_status", sa.String)
    )
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO exam_reports (request_id, finding, conclusion, critical,"
                " critical_status, reported_by, reported_at, created_at)"
                " VALUES (99901, '', '存量危急', 1, '', '旧系统', '2026-01-01 00:00:00',"
                " '2026-01-01 00:00:00')"
            )
        )
        conn.execute(
            exam_reports.update()
            .where(
                sa.and_(
                    exam_reports.c.critical == sa.true(),
                    sa.or_(
                        exam_reports.c.critical_status == "",
                        exam_reports.c.critical_status.is_(None),
                    ),
                )
            )
            .values(critical_status="notified")
        )
        status = conn.execute(
            sa.text("SELECT critical_status FROM exam_reports WHERE request_id = 99901")
        ).scalar()
        conn.execute(sa.text("DELETE FROM exam_reports WHERE request_id = 99901"))
    assert status == "notified"


# ---------------------------------------------------------------- M-2 定向通知


def test_critical_ws_directed_to_requesting_org(client, admin, setup):
    """危急值广播仅送达申请机构在线用户：他机构医师收不到无关机构消息。"""
    doc1 = setup["tokens"]["m1_doc1"]  # org1
    doc2 = setup["tokens"]["m1_doc2"]  # org2
    with client.websocket_connect(f"/ws/notifications?token={doc1}") as ws1:
        with client.websocket_connect(f"/ws/notifications?token={doc2}") as ws2:
            _make_critical_report(client, admin, setup, setup["org1"]["id"], "WS1")
            _make_critical_report(client, admin, setup, setup["org2"]["id"], "WS2")
            m1 = ws1.receive_json()
            assert m1["type"] == "critical_report"
            assert m1["org_id"] == setup["org1"]["id"]
            # org2 医师跳过了 org1 的危急值，第一条即 org2 消息
            m2 = ws2.receive_json()
            assert m2["org_id"] == setup["org2"]["id"]


def test_stock_shortage_ws_directed(client, admin, setup):
    """缺药预警定向：仅缺药机构（与监管角色）收到。"""
    client.post(
        "/api/pharmacy/stocks",
        json={
            "org_id": setup["org1"]["id"],
            "drug_code": "M1D",
            "drug_name": "定向测试药",
            "quantity": 10,
            "threshold": 9,
        },
        headers=admin,
    )
    doc1 = setup["tokens"]["m1_doc1"]  # org1（缺药机构）
    with client.websocket_connect(f"/ws/notifications?token={doc1}") as ws1:
        client.post(
            "/api/pharmacy/transfers",
            json={
                "drug_code": "M1D",
                "from_org_id": setup["org1"]["id"],
                "to_org_id": setup["org2"]["id"],
                "quantity": 5,
            },
            headers=admin,
        )
        msg = ws1.receive_json()
        assert msg["type"] == "stock_shortage"
        assert msg["org_id"] == setup["org1"]["id"]


# ---------------------------------------------------------------- M-3 portal 限速与 POST


def test_portal_verify_rate_limited(client, setup, monkeypatch):
    """同一证件号连续5次核验失败 → 锁定，正确凭据也临时拒绝（429）。

    旧核验接口默认已关（portal_legacy_verify=False，A2/P1-3），
    本条测的是限速逻辑本身，显式开启后再测。"""
    from app.config import settings

    monkeypatch.setattr(settings, "portal_legacy_verify", True)
    _reset_portal_failures()
    patient = setup["patient"]
    for _ in range(5):
        resp = client.get(
            f"/api/portal/my-archive?ehc_no={patient['ehc_no']}&id_card={patient['id_card'][:-1]}X"
        )
        assert resp.status_code == 403
    locked = client.get(
        f"/api/portal/my-archive?ehc_no={patient['ehc_no']}&id_card={patient['id_card'][:-1]}X"
    )
    assert locked.status_code == 429
    # 锁定期内正确凭据同样拒绝（同证件号维度）
    also_locked = client.post(
        "/api/portal/my-archive",
        json={"ehc_no": patient["ehc_no"], "id_card": patient["id_card"][:-1] + "X"},
    )
    assert also_locked.status_code == 429
    _reset_portal_failures()


def test_portal_my_archive_post_body(client, setup, monkeypatch):
    """POST body 传参（推荐）：不再经 URL 暴露身份证号。旧接口默认关，显式开启后测。"""
    from app.config import settings

    monkeypatch.setattr(settings, "portal_legacy_verify", True)
    _reset_portal_failures()
    patient = setup["patient"]
    resp = client.post(
        "/api/portal/my-archive",
        json={"ehc_no": patient["ehc_no"], "id_card": patient["id_card"]},
    )
    assert resp.status_code == 200
    assert resp.json()["ehc_no"] == patient["ehc_no"]


# ---------------------------------------------------------------- M-4 改密吊销令牌


def test_change_password_revokes_existing_tokens(client, admin):
    client.post(
        "/api/users",
        json={"username": "m1_reset", "password": "oldpass123", "role": "operator"},
        headers=admin,
    )
    token_a = login(client, "m1_reset", "oldpass123")
    token_b = login(client, "m1_reset", "oldpass123")
    assert client.get("/api/users/roles", headers=headers(token_b)).status_code == 200

    changed = client.post(
        "/api/auth/change-password",
        json={"current_password": "oldpass123", "new_password": "newpass456"},
        headers=headers(token_a),
    )
    assert changed.status_code == 200
    assert changed.json()["tokens_revoked"] is True

    # 改密前签发的全部令牌（含改密所用令牌）即时失效
    assert client.get("/api/users/roles", headers=headers(token_a)).status_code == 401
    assert client.get("/api/users/roles", headers=headers(token_b)).status_code == 401

    # 新密码登录的新令牌可用
    token_c = login(client, "m1_reset", "newpass456")
    assert client.get("/api/users/roles", headers=headers(token_c)).status_code == 200


# ---------------------------------------------------------------- M-5 待办/预警口径


def test_critical_todo_and_alert_clear_after_loop(client, admin, setup):
    """闭环完成后，待办与预警中的危急值口径同步清零（不永久累积）。"""

    def open_critical_count():
        todos = client.get("/api/todos", headers=admin).json()
        item = next(i for i in todos["items"] if i["type"] == "critical_report")
        alerts = client.get("/api/metrics/alerts", headers=admin).json()
        alert = next((i for i in alerts["items"] if i["type"] == "critical_values"), None)
        overview = client.get("/api/metrics/overview", headers=admin).json()
        return item["count"], (alert["count"] if alert else 0), overview["remote_diagnosis"]["critical_values"]

    before_todo, before_alert, before_overview = open_critical_count()
    req, report = _make_critical_report(client, admin, setup, setup["org1"]["id"], "LOOP")
    mid_todo, mid_alert, mid_overview = open_critical_count()
    assert mid_todo == before_todo + 1
    assert mid_alert == before_alert + 1
    assert mid_overview == before_overview + 1

    doc = headers(setup["tokens"]["m1_doc1"])
    client.post(f"/api/exams/reports/{report['id']}/acknowledge", headers=doc)
    client.post(f"/api/exams/reports/{report['id']}/resolve", json={"note": "处置完成"}, headers=doc)
    after_todo, after_alert, after_overview = open_critical_count()
    assert after_todo == before_todo
    assert after_alert == before_alert
    assert after_overview == before_overview


def test_doctor_todo_lists_unacknowledged_critical(client, admin, setup):
    req, report = _make_critical_report(client, admin, setup, setup["org1"]["id"], "DOCTODO")
    doc = headers(setup["tokens"]["m1_doc1"])
    todos = client.get("/api/todos", headers=doc).json()
    ack_item = next(i for i in todos["items"] if i["type"] == "critical_ack")
    assert any(r["id"] == report["id"] for r in ack_item["list"])
    client.post(f"/api/exams/reports/{report['id']}/acknowledge", headers=doc)
    todos = client.get("/api/todos", headers=doc).json()
    ack_item = next(i for i in todos["items"] if i["type"] == "critical_ack")
    assert all(r["id"] != report["id"] for r in ack_item["list"])


# ---------------------------------------------------------------- M-6 报告修订留痕


def test_report_amend_keeps_revision_history(client, admin, setup):
    req, report = _make_critical_report(client, admin, setup, setup["org1"]["id"], "AMEND")
    doc = headers(setup["tokens"]["m1_doc1"])
    # 先完成闭环
    client.post(f"/api/exams/reports/{report['id']}/acknowledge", headers=doc)
    client.post(f"/api/exams/reports/{report['id']}/resolve", json={"note": "ok"}, headers=doc)

    amended = client.patch(
        f"/api/exams/reports/{report['id']}",
        json={"conclusion": "复核后仍危急：血钾 6.8", "reason": "数值复核"},
        headers=doc,
    ).json()
    # 危急报告修订 → 闭环状态复位为已通知（须重新确认）
    assert amended["critical_status"] == "notified"

    revisions = client.get(f"/api/exams/reports/{report['id']}/revisions", headers=doc).json()
    assert len(revisions) == 1
    assert revisions[0]["prev_conclusion"] == "血钾 7.0 危急"
    assert revisions[0]["prev_critical"] is True
    assert revisions[0]["revised_by"] != ""
    assert revisions[0]["reason"] == "数值复核"

    # 留痕轨迹含"复位"动作
    actions = client.get(f"/api/exams/reports/{report['id']}/critical-actions", headers=doc).json()
    assert any("复位" in a["action"] for a in actions)

    # 修订解除危急标记 → 状态清空并留痕
    cleared = client.patch(
        f"/api/exams/reports/{report['id']}",
        json={"conclusion": "复核为误报，不构成危急值", "critical": False, "reason": "仪器误差"},
        headers=doc,
    ).json()
    assert cleared["critical"] is False
    assert cleared["critical_status"] == ""
    revisions = client.get(f"/api/exams/reports/{report['id']}/revisions", headers=doc).json()
    assert len(revisions) == 2
    actions = client.get(f"/api/exams/reports/{report['id']}/critical-actions", headers=doc).json()
    assert any("解除" in a["action"] for a in actions)


# ---------------------------------------------------------------- L-6/L-10 顺手清理


def test_claim_records_claimer_and_rejects_double_claim(client, admin, setup):
    req = client.post(
        "/api/exams",
        json={
            "patient_id": setup["patient"]["id"],
            "from_org_id": setup["org1"]["id"],
            "center_type": "imaging",
            "item_code": "CLAIM1",
            "item_name": "胸片",
        },
        headers=headers(setup["tokens"]["m1_doc1"]),
    ).json()
    first = client.post(
        f"/api/exams/{req['id']}/claim", headers=headers(setup["tokens"]["m1_doc1"])
    )
    assert first.status_code == 200
    assert first.json()["claimed_by"] != ""
    second = client.post(
        f"/api/exams/{req['id']}/claim", headers=headers(setup["tokens"]["m1_doc2"])
    )
    assert second.status_code == 409


def test_patient_registration_role_matrix(client, setup):
    body = {"name": "矩阵患者", "id_card": "320981199505055678"}
    denied = client.post(
        "/api/patients", json=body, headers=headers(setup["tokens"]["m1_pharm"])
    )
    assert denied.status_code == 403
    allowed = client.post(
        "/api/patients", json=body, headers=headers(setup["tokens"]["m1_op"])
    )
    assert allowed.status_code == 201
