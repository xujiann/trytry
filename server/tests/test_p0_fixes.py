"""第三阶段 P0 整改回归测试：H1-H4 高危与 M1-M6 中危、L1 修复验证。"""
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import reset_database

from app.main import app


# M2 测试专用路由：模拟业务路由抛未捕获异常（500）
@app.post("/api/_test/boom", include_in_schema=False)
def _boom():
    raise RuntimeError("boom")


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
def setup(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "P0县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    township = client.post(
        "/api/organizations",
        json={"name": "P0卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "整改患者", "id_card": "320981199101012222", "phone": "13900001111"},
        headers=admin,
    ).json()
    roles = {}
    for username, role in [
        ("p0_doc", "doctor"),
        ("p0_op", "operator"),
        ("p0_ph", "public_health"),
        ("p0_pharm", "pharmacist"),
    ]:
        resp = client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
        assert resp.status_code == 201, resp.text
        roles[role] = login(client, username, "pass123456")
    return {"org": org, "township": township, "patient": patient, **roles}


# ---------------------------------------------------------------- H4 默认密钥


def test_prod_rejects_default_secret(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("MEDPLAT_ENVIRONMENT", "prod")
    with pytest.raises(RuntimeError, match="MEDPLAT_SECRET"):
        Settings()


def test_prod_rejects_default_admin_password(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("MEDPLAT_ENV", "prod")
    monkeypatch.setenv("MEDPLAT_SECRET", "a-strong-random-secret-0123456789")
    with pytest.raises(RuntimeError, match="MEDPLAT_ADMIN_PASSWORD"):
        Settings()


def test_prod_with_proper_credentials_starts(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("MEDPLAT_ENVIRONMENT", "prod")
    monkeypatch.setenv("MEDPLAT_SECRET", "a-strong-random-secret-0123456789")
    monkeypatch.setenv("MEDPLAT_ADMIN_PASSWORD", "Str0ngPassw0rd!")
    settings = Settings()
    assert settings.is_production


def test_dev_allows_defaults(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("MEDPLAT_ENVIRONMENT", raising=False)
    monkeypatch.delenv("MEDPLAT_ENV", raising=False)
    assert Settings().is_production is False


# ---------------------------------------------------------------- H1 脱敏统一


def test_mask_helpers():
    from app.privacy import mask_id_card, mask_phone

    assert mask_id_card("320981199001011111") == "3209**********1111"
    assert mask_phone("13899998888") == "138******88"
    assert mask_id_card("short") == "short"
    assert mask_phone("12345") == "12345"


def test_fhir_export_masked_for_non_admin(client, admin, setup):
    ehc_no = setup["patient"]["ehc_no"]
    # operator 属对接角色，导出必须脱敏
    op_view = client.get(f"/api/integration/fhir/Patient/{ehc_no}", headers=setup["operator"]).json()
    values = [i["value"] for i in op_view["identifier"]]
    assert "3209**********2222" in values
    assert "320981199101012222" not in values
    assert op_view["telecom"][0]["value"] == "139******11"
    # admin 保留明文
    admin_view = client.get(f"/api/integration/fhir/Patient/{ehc_no}", headers=admin).json()
    assert "320981199101012222" in [i["value"] for i in admin_view["identifier"]]


# ---------------------------------------------------------------- H2 越权矩阵


def test_operator_cannot_do_clinical_operations(client, admin, setup):
    p_id, org, township = setup["patient"]["id"], setup["org"], setup["township"]

    # 转诊：operator 可申请，不可接诊流转
    referral = client.post(
        "/api/referrals",
        json={
            "patient_id": p_id,
            "from_org_id": township["id"],
            "to_org_id": org["id"],
            "direction": "up",
            "reason": "上转",
        },
        headers=setup["operator"],
    )
    assert referral.status_code == 201
    rid = referral.json()["id"]
    denied = client.patch(
        f"/api/referrals/{rid}/status", json={"status": "accepted"}, headers=setup["operator"]
    )
    assert denied.status_code == 403
    accepted = client.patch(
        f"/api/referrals/{rid}/status", json={"status": "accepted"}, headers=setup["doctor"]
    )
    assert accepted.status_code == 200

    # 处方开具限医师
    rx_body = {
        "patient_id": p_id,
        "org_id": org["id"],
        "diagnosis_name": "高血压",
        "items": [{"drug_code": "P0DRUG", "drug_name": "整改测试药", "daily_dose": 1, "days": 7}],
    }
    assert client.post("/api/prescriptions", json=rx_body, headers=setup["operator"]).status_code == 403
    assert client.post("/api/prescriptions", json=rx_body, headers=setup["doctor"]).status_code == 201

    # 慢病建档/随访限 doctor/public_health
    chronic_body = {"patient_id": p_id, "disease": "hypertension", "managed_by_org_id": org["id"]}
    assert client.post("/api/chronic", json=chronic_body, headers=setup["operator"]).status_code == 403
    chronic = client.post("/api/chronic", json=chronic_body, headers=setup["public_health"])
    assert chronic.status_code == 201
    cid = chronic.json()["id"]
    fu = {"sbp": 150, "dbp": 95}
    assert client.post(f"/api/chronic/{cid}/followups", json=fu, headers=setup["operator"]).status_code == 403
    assert client.post(f"/api/chronic/{cid}/followups", json=fu, headers=setup["public_health"]).status_code == 201

    # 传染病报告限 doctor/public_health
    case_body = {
        "org_id": org["id"],
        "disease_code": "B01",
        "disease_name": "水痘",
        "onset_date": "2026-08-01",
    }
    assert client.post("/api/infectious/cases", json=case_body, headers=setup["operator"]).status_code == 403
    assert client.post("/api/infectious/cases", json=case_body, headers=setup["doctor"]).status_code == 201


def test_role_matrix_emergency_and_insurance(client, setup):
    # 急救调度=operator/doctor，药师不可
    case_body = {"location": "P0广场", "symptom": "晕厥"}
    assert client.post("/api/emergency/cases", json=case_body, headers=setup["pharmacist"]).status_code == 403
    case = client.post("/api/emergency/cases", json=case_body, headers=setup["operator"])
    assert case.status_code == 201

    # 医保结算=operator，医师不可
    settle_body = {
        "patient_id": setup["patient"]["id"],
        "org_id": setup["org"]["id"],
        "settle_type": "local",
        "total_amount": 100.0,
        "insurance_pay": 60.0,
        "self_pay": 40.0,
    }
    assert client.post("/api/insurance/settlements", json=settle_body, headers=setup["doctor"]).status_code == 403
    assert client.post("/api/insurance/settlements", json=settle_body, headers=setup["operator"]).status_code == 201


def test_role_matrix_publichealth_records(client, setup):
    # 疫苗接种登记限 doctor/public_health（L5 整改）
    rec = {
        "patient_id": setup["patient"]["id"],
        "org_id": setup["org"]["id"],
        "vaccine_code": "HEPB",
        "vaccine_name": "乙肝疫苗",
        "dose_no": 1,
    }
    assert client.post("/api/vaccination/records", json=rec, headers=setup["operator"]).status_code == 403
    assert client.post("/api/vaccination/records", json=rec, headers=setup["public_health"]).status_code == 201


# ---------------------------------------------------------------- H3 并发原子性 + M1 状态机


@pytest.fixture()
def slot(client, admin, setup):
    return client.post(
        "/api/appointments/slots",
        json={
            "org_id": setup["org"]["id"],
            "resource_type": "outpatient",
            "resource_name": "P0门诊",
            "slot_date": "2026-09-10",
            "capacity": 2,
        },
        headers=admin,
    ).json()


def _make_patient(client, admin, id_card, name):
    return client.post(
        "/api/patients", json={"name": name, "id_card": id_card}, headers=admin
    ).json()


def test_slot_conditional_update_prevents_overbook(client, admin, setup, slot):
    p1 = _make_patient(client, admin, "320981199201013333", "并发甲")
    p2 = _make_patient(client, admin, "320981199201014444", "并发乙")
    p3 = _make_patient(client, admin, "320981199201015555", "并发丙")

    for p in (p1, p2):
        resp = client.post(
            "/api/appointments",
            json={"slot_id": slot["id"], "patient_id": p["id"]},
            headers=setup["operator"],
        )
        assert resp.status_code == 201

    # 条件更新失败路径：booked==capacity 时第 3 人约不进（影响行数为 0 → 409）
    full = client.post(
        "/api/appointments",
        json={"slot_id": slot["id"], "patient_id": p3["id"]},
        headers=setup["operator"],
    )
    assert full.status_code == 409
    slots = client.get(f"/api/appointments/slots?slot_date=2026-09-10", headers=admin).json()
    current = next(s for s in slots if s["id"] == slot["id"])
    assert current["booked"] == 2  # 未超卖


def test_cancel_releases_slot_without_negative(client, admin, setup, slot):
    p = _make_patient(client, admin, "320981199201016666", "并发丁")
    appt = client.post(
        "/api/appointments",
        json={"slot_id": slot["id"], "patient_id": p["id"]},
        headers=setup["operator"],
    ).json()
    assert client.post(f"/api/appointments/{appt['id']}/cancel", headers=setup["operator"]).status_code == 200
    # 重复取消被状态机拒绝，不会再次释放号源
    assert client.post(f"/api/appointments/{appt['id']}/cancel", headers=setup["operator"]).status_code == 409
    slots = client.get(f"/api/appointments/slots?slot_date=2026-09-10", headers=admin).json()
    current = next(s for s in slots if s["id"] == slot["id"])
    assert current["booked"] >= 0


def test_fulfilled_appointment_cannot_be_rebooked(client, admin, setup, slot):
    """M1 整改：已就诊预约不可被静默回退复用。"""
    p = _make_patient(client, admin, "320981199201017777", "并发戊")
    appt = client.post(
        "/api/appointments",
        json={"slot_id": slot["id"], "patient_id": p["id"]},
        headers=setup["operator"],
    ).json()
    assert client.post(f"/api/appointments/{appt['id']}/fulfill", headers=setup["operator"]).status_code == 200

    before = next(
        s for s in client.get(f"/api/appointments/slots?slot_date=2026-09-10", headers=admin).json()
        if s["id"] == slot["id"]
    )["booked"]
    rebook = client.post(
        "/api/appointments",
        json={"slot_id": slot["id"], "patient_id": p["id"]},
        headers=setup["operator"],
    )
    assert rebook.status_code == 409
    # 就诊状态未被回退、号源占用未翻倍
    appts = client.get(f"/api/appointments?patient_id={p['id']}", headers=admin).json()
    assert appts[0]["status"] == "fulfilled"
    after = next(
        s for s in client.get(f"/api/appointments/slots?slot_date=2026-09-10", headers=admin).json()
        if s["id"] == slot["id"]
    )["booked"]
    assert after == before

    # 已就诊也不可取消
    assert client.post(f"/api/appointments/{appt['id']}/cancel", headers=setup["operator"]).status_code == 409


def test_stock_transfer_conditional_update_never_negative(client, admin, setup):
    client.post(
        "/api/pharmacy/stocks",
        json={
            "org_id": setup["org"]["id"],
            "drug_code": "P0STOCK",
            "drug_name": "库存整改药",
            "quantity": 10,
            "threshold": 0,
        },
        headers=admin,
    )
    body = {
        "drug_code": "P0STOCK",
        "from_org_id": setup["org"]["id"],
        "to_org_id": setup["township"]["id"],
        "quantity": 15,
    }
    # 条件更新失败路径：需求量>库存 → 409，库存不变
    assert client.post("/api/pharmacy/transfers", json=body, headers=setup["operator"]).status_code == 409
    stocks = client.get(f"/api/pharmacy/stocks?org_id={setup['org']['id']}", headers=admin).json()
    src = next(s for s in stocks if s["drug_code"] == "P0STOCK")
    assert src["quantity"] == 10

    # 全量调出后归零，再调 → 409（不会为负）
    assert client.post(
        "/api/pharmacy/transfers", json=dict(body, quantity=10), headers=setup["operator"]
    ).status_code == 201
    stocks = client.get(f"/api/pharmacy/stocks?org_id={setup['org']['id']}", headers=admin).json()
    src = next(s for s in stocks if s["drug_code"] == "P0STOCK")
    assert src["quantity"] == 0
    assert client.post(
        "/api/pharmacy/transfers", json=dict(body, quantity=1), headers=setup["operator"]
    ).status_code == 409


# ---------------------------------------------------------------- M2 审计覆盖 5xx


def test_audit_records_500_on_unhandled_exception(client, admin):
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post("/api/_test/boom", headers=admin)
        assert resp.status_code == 500
    logs = client.get("/api/audit?limit=50", headers=admin).json()
    boom = [l for l in logs if l["path"] == "/api/_test/boom"]
    assert boom and boom[0]["status_code"] == 500
    assert boom[0]["username"] == "admin"


# ---------------------------------------------------------------- M5 字典读路径只读


def test_dictionary_read_has_no_write_side_effect(client, admin):
    from app.database import SessionLocal
    from app.models import CodeSystem

    db = SessionLocal()
    try:
        before = db.query(CodeSystem).count()
        assert before >= 4  # 启动种子化四类字典
    finally:
        db.close()

    assert client.get("/api/dictionaries/diagnosis/entries", headers=admin).status_code == 200
    assert client.get("/api/dictionaries/drug/entries", headers=admin).status_code == 200
    assert client.get("/api/dictionaries/nonsense/entries", headers=admin).status_code == 404

    db = SessionLocal()
    try:
        assert db.query(CodeSystem).count() == before  # 读操作零写副作用
    finally:
        db.close()


# ---------------------------------------------------------------- M6 并发建档幂等


def test_patient_register_race_falls_back_to_existing(client, admin, monkeypatch):
    import app.routers.patients as patients_mod

    first = client.post(
        "/api/patients", json={"name": "竞态患者", "id_card": "320981199301018888"}, headers=admin
    ).json()

    # 模拟竞态：首次查重返回 None（另一请求已抢先插入），触发唯一约束冲突兜底
    real_find = patients_mod._find_by_id_card
    calls = {"n": 0}

    def racy_find(db, id_card):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_find(db, id_card)

    monkeypatch.setattr(patients_mod, "_find_by_id_card", racy_find)
    resp = client.post(
        "/api/patients", json={"name": "竞态患者", "id_card": "320981199301018888"}, headers=admin
    )
    assert resp.status_code == 201  # 幂等返回既有档案而非 500
    assert resp.json()["ehc_no"] == first["ehc_no"]
    assert calls["n"] >= 2


# ---------------------------------------------------------------- M3 WS 首帧鉴权与周期复核


def test_ws_first_frame_auth(client, admin):
    from app.ws import manager

    token = admin["Authorization"].split(" ", 1)[1]
    # 首帧鉴权：URL 不带 token，第一条文本帧发送令牌
    with client.websocket_connect("/ws/notifications") as ws:
        ws.send_text(token)
        time.sleep(0.2)
        assert len(manager.active) == 1
    # 首帧无效令牌被拒
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/notifications") as ws:
            ws.send_text("bad-token")
            ws.receive_text()


def test_ws_heartbeat_revalidates_revoked_token(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    with client.websocket_connect(f"/ws/notifications?token={token}") as ws:
        # 登出拉黑后，下一次心跳复核即断开（M3 整改）
        client.post("/api/auth/logout", headers=headers)
        ws.send_text("ping")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


# ---------------------------------------------------------------- M4 状态存储


def test_token_blacklist_expires_entries():
    from app.state_store import TokenBlacklist

    bl = TokenBlacklist(default_ttl_seconds=3600)
    bl.add("tok-a")
    assert "tok-a" in bl
    bl._memory["tok-a"] = time.time() - 1  # 模拟到期
    assert "tok-a" not in bl


def test_login_failure_tracker_lock_and_reset():
    from app.state_store import LoginFailureTracker

    tracker = LoginFailureTracker(fail_limit=3, lock_seconds=60)
    assert tracker.record_failure("u1") is False
    assert tracker.record_failure("u1") is False
    assert tracker.record_failure("u1") is True  # 第3次触发锁定
    assert tracker.locked_remaining("u1") > 0
    tracker.reset("u1")
    assert tracker.locked_remaining("u1") == 0


# ---------------------------------------------------------------- L1 互认窗口建单侧复核


def test_recognition_window_enforced_at_creation(client, admin, setup):
    from datetime import datetime, timedelta, timezone

    from app.database import SessionLocal
    from app.models import ExamReport

    req = client.post(
        "/api/exams",
        json={
            "patient_id": setup["patient"]["id"],
            "from_org_id": setup["township"]["id"],
            "center_type": "lab",
            "item_code": "P0LAB",
            "item_name": "血常规",
        },
        headers=setup["doctor"],
    ).json()
    report = client.post(
        f"/api/exams/{req['id']}/report",
        json={"conclusion": "正常"},
        headers=setup["doctor"],
    )
    assert report.status_code == 201

    # 窗口内：可互认建单
    ok = client.post(
        "/api/exams",
        json={
            "patient_id": setup["patient"]["id"],
            "from_org_id": setup["org"]["id"],
            "center_type": "lab",
            "item_code": "P0LAB",
            "item_name": "血常规",
            "accept_recognition_of": req["id"],
        },
        headers=setup["doctor"],
    )
    assert ok.status_code == 201
    assert ok.json()["status"] == "recognized"

    # 把报告时间改到 40 天前 → 建单侧拒绝互认（L1 整改）
    db = SessionLocal()
    try:
        row = db.query(ExamReport).filter(ExamReport.request_id == req["id"]).first()
        row.reported_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=40)
        db.commit()
    finally:
        db.close()

    stale = client.post(
        "/api/exams",
        json={
            "patient_id": setup["patient"]["id"],
            "from_org_id": setup["org"]["id"],
            "center_type": "lab",
            "item_code": "P0LAB",
            "item_name": "血常规",
            "accept_recognition_of": req["id"],
        },
        headers=setup["doctor"],
    )
    assert stale.status_code == 422
    assert "互认窗口" in stale.json()["detail"]
