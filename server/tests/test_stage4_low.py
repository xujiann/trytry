"""第四阶段 块1：遗留低危项（L 系列）回归测试。

覆盖：L-3 分页与 X-Total-Count、L-2 today 参数校验、L-4/L-5 互认 scope 与
center_type 判定、L-8 登录失败滑动窗口、L-9 jti 黑名单、L-11 特病审核职责
分离、L-12 绿道时间/体质分值校验、L-1 绩效口径参数化。
"""
import time

import pytest

from app.security import decode_token, revoked_tokens
from app.state_store import LoginFailureTracker


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
    county = client.post(
        "/api/organizations",
        json={"name": "L县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    city = client.post(
        "/api/organizations",
        json={"name": "L市医院", "org_type": "lead_hospital", "level": "city"},
        headers=admin,
    ).json()
    patients = []
    for i in range(3):
        patients.append(
            client.post(
                "/api/patients",
                json={"name": f"L患者{i}", "id_card": f"32098119900101{1000 + i}"},
                headers=admin,
            ).json()
        )
    users = {}
    for username, role in [
        ("l_op", "operator"),
        ("l_director", "director"),
    ]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": county["id"]},
            headers=admin,
        )
        users[username] = login(client, username, "pass123456")
    return {"county": county, "city": city, "patients": patients, "tokens": users}


# ---------------------------------------------------------------- L-3 分页


def test_pagination_patients(client, admin, setup):
    resp = client.get("/api/patients", headers=admin)
    assert resp.status_code == 200
    total = int(resp.headers["X-Total-Count"])
    assert total >= 3 and len(resp.json()) == total

    page = client.get("/api/patients?offset=1&limit=1", headers=admin)
    assert page.status_code == 200
    assert int(page.headers["X-Total-Count"]) == total
    assert len(page.json()) == 1
    # offset=1 返回第二条（按 id 升序）
    assert page.json()[0]["id"] == resp.json()[1]["id"]


def test_pagination_audit_and_lists(client, admin, setup):
    audit = client.get("/api/audit?limit=2", headers=admin)
    assert audit.status_code == 200
    assert "X-Total-Count" in audit.headers
    assert len(audit.json()) <= 2

    for path in ("/api/encounters", "/api/exams", "/api/prescriptions", "/api/chronic",
                 "/api/insurance/settlements"):
        resp = client.get(f"{path}?offset=0&limit=5", headers=admin)
        assert resp.status_code == 200, (path, resp.text)
        assert "X-Total-Count" in resp.headers, path


# ---------------------------------------------------------------- L-2 today 参数


def test_today_param_validation(client, admin):
    assert client.get("/api/chronic/overdue?today=bad-date", headers=admin).status_code == 422
    assert client.get("/api/medwaste/alerts?today=2026/01/01", headers=admin).status_code == 422
    assert client.get("/api/infectious/alerts?today=20260101x", headers=admin).status_code == 422
    assert (
        client.get("/api/exams/critical/unacknowledged?today=notadate", headers=admin).status_code
        == 422
    )
    # 缺省（服务端当前日期）与合法覆盖值均正常
    assert client.get("/api/chronic/overdue", headers=admin).status_code == 200
    assert client.get("/api/chronic/overdue?today=2026-08-11", headers=admin).status_code == 200


# ---------------------------------------------------------------- L-4/L-5 互认判定


def test_recognition_scope_and_center_type(client, admin, setup):
    pid = setup["patients"][0]["id"]
    # 目录：仅影像项目 RT1，县域互认
    item = client.post(
        "/api/exams/recognition-items",
        json={"item_code": "RT1", "item_name": "胸部CT", "center_type": "imaging",
              "mutual_scope": "county"},
        headers=admin,
    ).json()

    # 源报告：市级医院出具
    src = client.post(
        "/api/exams",
        json={"patient_id": pid, "from_org_id": setup["city"]["id"],
              "center_type": "imaging", "item_code": "RT1", "item_name": "胸部CT"},
        headers=admin,
    ).json()
    rep = client.post(
        f"/api/exams/{src['id']}/report",
        json={"finding": "未见异常", "conclusion": "正常", "reported_by": "市院医师"},
        headers=admin,
    )
    assert rep.status_code == 201

    # L-5：目录 center_type 参与预检判定
    chk = client.get(
        f"/api/exams/recognition-check?patient_id={pid}&item_code=RT1&center_type=ecg",
        headers=admin,
    ).json()
    assert chk["recognizable"] is False and "中心类型" in chk["reason"]

    # L-5：建单侧中心类型不一致 → 422
    bad_center = client.post(
        "/api/exams",
        json={"patient_id": pid, "from_org_id": setup["county"]["id"],
              "center_type": "ecg", "item_code": "RT1", "item_name": "胸部CT",
              "accept_recognition_of": src["id"]},
        headers=admin,
    )
    assert bad_center.status_code == 422

    # L-4：county 范围目录项，源报告机构为市级 → 阻断
    blocked = client.post(
        "/api/exams",
        json={"patient_id": pid, "from_org_id": setup["county"]["id"],
              "center_type": "imaging", "item_code": "RT1", "item_name": "胸部CT",
              "accept_recognition_of": src["id"]},
        headers=admin,
    )
    assert blocked.status_code == 422 and "县域" in blocked.json()["detail"]

    # 调整目录为市级互认后放行
    client.patch(
        f"/api/exams/recognition-items/{item['id']}",
        json={"mutual_scope": "city"},
        headers=admin,
    )
    ok = client.post(
        "/api/exams",
        json={"patient_id": pid, "from_org_id": setup["county"]["id"],
              "center_type": "imaging", "item_code": "RT1", "item_name": "胸部CT",
              "accept_recognition_of": src["id"]},
        headers=admin,
    )
    assert ok.status_code == 201 and ok.json()["status"] == "recognized"


# ---------------------------------------------------------------- L-8 滑动窗口


def test_login_failure_sliding_window():
    tracker = LoginFailureTracker(fail_limit=3, lock_seconds=1)
    # 零散失败：窗口过期后计数清零，不触发锁定
    assert tracker.record_failure("u1") is False
    assert tracker.record_failure("u1") is False
    time.sleep(1.1)
    assert tracker.record_failure("u1") is False
    assert tracker.record_failure("u1") is False
    # 窗口内连续失败达到阈值 → 锁定
    assert tracker.record_failure("u1") is True

    # 窗口较长时锁定剩余时间可查询
    slow = LoginFailureTracker(fail_limit=2, lock_seconds=600)
    slow.record_failure("u2")
    assert slow.record_failure("u2") is True
    assert slow.locked_remaining("u2") > 0


# ---------------------------------------------------------------- L-9 jti 黑名单


def test_logout_blacklists_jti_only(client, admin, setup):
    t1 = login(client, "l_op", "pass123456")
    t2 = login(client, "l_op", "pass123456")
    assert client.post("/api/auth/logout", headers=headers(t1)).status_code == 200
    # 被登出令牌失效；同用户其他令牌不受影响
    assert client.get("/api/users/roles", headers=headers(t1)).status_code == 401
    assert client.get("/api/users/roles", headers=headers(t2)).status_code == 200
    # 存储中按 jti 拉黑，不落完整令牌明文
    jti = decode_token(t1)["jti"]
    assert jti in revoked_tokens
    assert t1 not in revoked_tokens


# ---------------------------------------------------------------- L-11 职责分离


def test_special_disease_review_separation(client, admin, setup):
    op = headers(setup["tokens"]["l_op"])
    director = headers(setup["tokens"]["l_director"])
    app_ = client.post(
        "/api/insurance/special-diseases",
        json={"patient_id": setup["patients"][0]["id"], "disease_name": "尿毒症透析"},
        headers=op,
    ).json()
    # 经办可申报但不可审核（自报自批被阻断）
    denied = client.post(
        f"/api/insurance/special-diseases/{app_['id']}/review?approve=true", headers=op
    )
    assert denied.status_code == 403
    approved = client.post(
        f"/api/insurance/special-diseases/{app_['id']}/review?approve=true", headers=director
    )
    assert approved.status_code == 200 and approved.json()["status"] == "approved"


# ---------------------------------------------------------------- L-12 校验加固


def test_emergency_milestone_validation(client, admin):
    case = client.post(
        "/api/emergency/cases",
        json={"location": "L村", "channel_type": "chest_pain"},
        headers=admin,
    ).json()
    cid = case["id"]
    # 非法时间格式 → 422
    bad = client.post(
        f"/api/emergency/cases/{cid}/milestones",
        json={"milestone": "onset", "occurred_at": "昨天下午"},
        headers=admin,
    )
    assert bad.status_code == 422
    assert (
        client.post(
            f"/api/emergency/cases/{cid}/milestones",
            json={"milestone": "onset", "occurred_at": "2026-08-10 14:00"},
            headers=admin,
        ).status_code
        == 201
    )
    # 时序矛盾："呼救"早于"发病" → 422
    disorder = client.post(
        f"/api/emergency/cases/{cid}/milestones",
        json={"milestone": "call", "occurred_at": "2026-08-10 13:00"},
        headers=admin,
    )
    assert disorder.status_code == 422 and "时序矛盾" in disorder.json()["detail"]
    assert (
        client.post(
            f"/api/emergency/cases/{cid}/milestones",
            json={"milestone": "call", "occurred_at": "2026-08-10 14:05"},
            headers=admin,
        ).status_code
        == 201
    )


def test_tcm_scores_range(client, admin):
    bad = client.post(
        "/api/tcm/constitution", json={"scores": {"qi_deficiency": 150}}, headers=admin
    )
    assert bad.status_code == 422
    ok = client.post(
        "/api/tcm/constitution", json={"scores": {"qi_deficiency": 55}}, headers=admin
    )
    assert ok.status_code == 200 and ok.json()["constitution"] == "气虚质"


# ---------------------------------------------------------------- L-1 绩效口径参数


def test_performance_scorecard_params(client, setup):
    director = headers(setup["tokens"]["l_director"])
    assert client.get("/api/performance/orgs?volume_cap=0", headers=director).status_code == 422
    default = client.get("/api/performance/orgs", headers=director)
    strict = client.get(
        "/api/performance/orgs?volume_cap=10&include_auto_passed=false", headers=director
    )
    assert default.status_code == 200 and strict.status_code == 200
    assert "scorecards" in strict.json()
