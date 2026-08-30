"""深化轮（1-3）：互认目录化、危急值闭环、审方规则深化。"""
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
def setup(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "深化县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    township = client.post(
        "/api/organizations",
        json={"name": "深化卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    adult = client.post(
        "/api/patients",
        json={"name": "深化成人", "id_card": "320981199001013333", "birth_date": "1990-01-01"},
        headers=admin,
    ).json()
    child = client.post(
        "/api/patients",
        json={"name": "深化儿童", "id_card": "320981201805014444", "birth_date": "2018-05-01"},
        headers=admin,
    ).json()
    elderly = client.post(
        "/api/patients",
        json={"name": "深化老人", "id_card": "320981195003015555", "birth_date": "1950-03-01"},
        headers=admin,
    ).json()
    roles = {}
    for username, role in [("dp_doc", "doctor"), ("dp_op", "operator"), ("dp_pharm", "pharmacist")]:
        resp = client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
        assert resp.status_code == 201, resp.text
        roles[role] = login(client, username, "pass123456")
    return {
        "org": org,
        "township": township,
        "adult": adult,
        "child": child,
        "elderly": elderly,
        **roles,
    }


def _order_exam(client, headers, patient_id, org_id, item_code, item_name, center_type="imaging", **extra):
    return client.post(
        "/api/exams",
        json={
            "patient_id": patient_id,
            "from_org_id": org_id,
            "center_type": center_type,
            "item_code": item_code,
            "item_name": item_name,
            **extra,
        },
        headers=headers,
    )


# ---------------------------------------------------------------- 1 互认目录化


def test_recognition_directory_admin_only(client, setup):
    body = {"item_code": "RBAC-X", "item_name": "越权项目", "center_type": "imaging"}
    assert client.post("/api/exams/recognition-items", json=body, headers=setup["doctor"]).status_code == 403


def test_recognition_directory_gating(client, admin, setup):
    doc = setup["doctor"]
    pid, org = setup["adult"]["id"], setup["township"]["id"]

    # 管理员维护目录：CT01 在目录且启用；LAB99 在目录但停用
    ct = client.post(
        "/api/exams/recognition-items",
        json={"item_code": "CT01", "item_name": "胸部CT", "center_type": "imaging", "mutual_scope": "city"},
        headers=admin,
    )
    assert ct.status_code == 201
    assert ct.json()["mutual_scope"] == "city"
    client.post(
        "/api/exams/recognition-items",
        json={"item_code": "LAB99", "item_name": "停用检验", "center_type": "lab", "active": False},
        headers=admin,
    ).json()
    # 重复编码冲突
    assert (
        client.post(
            "/api/exams/recognition-items",
            json={"item_code": "CT01", "item_name": "重复", "center_type": "imaging"},
            headers=admin,
        ).status_code
        == 409
    )

    # 三个项目均已有报告
    reports = {}
    for code, name, ctype in [
        ("CT01", "胸部CT", "imaging"),
        ("LAB99", "停用检验", "lab"),
        ("XR01", "目录外X光", "imaging"),
    ]:
        req = _order_exam(client, doc, pid, org, code, name, ctype).json()
        resp = client.post(f"/api/exams/{req['id']}/report", json={"conclusion": "未见异常"}, headers=doc)
        assert resp.status_code == 201
        reports[code] = req

    # 仅目录内 active 项目提示互认
    ok = client.get(f"/api/exams/recognition-check?patient_id={pid}&item_code=CT01", headers=doc).json()
    assert ok["recognizable"] is True
    inactive = client.get(
        f"/api/exams/recognition-check?patient_id={pid}&item_code=LAB99", headers=doc
    ).json()
    assert inactive["recognizable"] is False and "停用" in inactive["reason"]
    outside = client.get(
        f"/api/exams/recognition-check?patient_id={pid}&item_code=XR01", headers=doc
    ).json()
    assert outside["recognizable"] is False and "不在互认目录" in outside["reason"]

    # 建单侧同样管控：目录外项目互认建单 → 422
    blocked = _order_exam(
        client, doc, pid, org, "XR01", "目录外X光", "imaging",
        accept_recognition_of=reports["XR01"]["id"],
    )
    assert blocked.status_code == 422 and "不在互认目录" in blocked.json()["detail"]

    # 目录内项目互认建单成功
    recognized = _order_exam(
        client, doc, pid, org, "CT01", "胸部CT", "imaging",
        accept_recognition_of=reports["CT01"]["id"],
    )
    assert recognized.status_code == 201
    assert recognized.json()["status"] == "recognized"

    # PATCH 停用后不再提示互认
    item_id = ct.json()["id"]
    assert (
        client.patch(
            f"/api/exams/recognition-items/{item_id}", json={"active": False}, headers=admin
        ).json()["active"]
        is False
    )
    off = client.get(f"/api/exams/recognition-check?patient_id={pid}&item_code=CT01", headers=doc).json()
    assert off["recognizable"] is False
    client.patch(f"/api/exams/recognition-items/{item_id}", json={"active": True}, headers=admin)


def test_recognition_stats(client, admin, setup):
    stats = client.get("/api/exams/recognition-stats", headers=admin).json()
    # 上一用例：3 张已报告 + 1 张互认
    assert stats["reported_total"] == 3
    assert stats["recognized_total"] == 1
    assert stats["saved_exams"] == 1
    assert stats["recognition_ratio_pct"] == 25.0
    by_item = {r["item_code"]: r["recognized_count"] for r in stats["by_item"]}
    assert by_item == {"CT01": 1}


# ---------------------------------------------------------------- 2 危急值闭环


def test_critical_closed_loop(client, admin, setup):
    doc = setup["doctor"]
    pid, org = setup["adult"]["id"], setup["township"]["id"]

    req = _order_exam(client, doc, pid, org, "K-CRIT", "血钾", "lab").json()
    # 先「领取」再出报告：`dp_doc` 属县医院，而这张单是卫生院开的（共享诊断中心的
    # 常态）。领取会把 `claimed_org_id` 落成中心机构，中心与该患者的服务关系
    # 正是靠这一列成立的（visibility._relation_tables 的后缀推导）——平台当初加
    # 这一列，就是为了修「中心医师写完报告打不开自己写的那份」。
    # 原先这条用例跳过了领取，于是危急值闭环的三个动作都由一个与该患者
    # **没有任何服务关系**的县医院医师完成，只是当时闭环上没有归属守卫才绿着。
    client.post(f"/api/exams/{req['id']}/claim", headers=doc)
    report = client.post(
        f"/api/exams/{req['id']}/report",
        json={"conclusion": "血钾 7.1 mmol/L 危急", "critical": True, "reported_by": "检验科张主任"},
        headers=doc,
    ).json()
    # critical=true 自动 notified 并留痕
    assert report["critical_status"] == "notified"
    rid = report["id"]

    # 超时未确认清单（today 晚于报告日期 → 计入）
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    overdue = client.get(f"/api/exams/critical/unacknowledged?today={tomorrow}", headers=admin).json()
    assert any(r["report_id"] == rid for r in overdue)
    # 默认 30 分钟超时口径下（刚发布）不应计入
    assert client.get("/api/exams/critical/unacknowledged", headers=admin).json() == []

    # 未确认前不可处置反馈
    assert client.post(f"/api/exams/reports/{rid}/resolve", json={"note": "补钾"}, headers=doc).status_code == 409
    # 经办人员无权确认（诊疗性质操作限医师）
    assert client.post(f"/api/exams/reports/{rid}/acknowledge", headers=setup["operator"]).status_code == 403

    acked = client.post(f"/api/exams/reports/{rid}/acknowledge", headers=doc)
    assert acked.status_code == 200 and acked.json()["critical_status"] == "acknowledged"
    # 重复确认 → 409；确认后不再出现在超时清单
    assert client.post(f"/api/exams/reports/{rid}/acknowledge", headers=doc).status_code == 409
    assert client.get(f"/api/exams/critical/unacknowledged?today={tomorrow}", headers=admin).json() == []

    resolved = client.post(
        f"/api/exams/reports/{rid}/resolve", json={"note": "已静脉补液并复查"}, headers=doc
    )
    assert resolved.status_code == 200 and resolved.json()["critical_status"] == "resolved"

    # 处置留痕：通知→确认→处置 三步全记录
    actions = client.get(f"/api/exams/reports/{rid}/critical-actions", headers=admin).json()
    assert len(actions) == 3
    assert "已通知" in actions[0]["action"]
    assert "确认接收" in actions[1]["action"]
    assert "已静脉补液" in actions[2]["action"]
    assert actions[0]["actor"] == "检验科张主任"


def test_non_critical_report_needs_no_loop(client, admin, setup):
    doc = setup["doctor"]
    req = _order_exam(
        client, doc, setup["adult"]["id"], setup["township"]["id"], "ECG01", "常规心电图", "ecg"
    ).json()
    report = client.post(
        f"/api/exams/{req['id']}/report", json={"conclusion": "窦性心律"}, headers=doc
    ).json()
    assert report["critical_status"] == ""
    assert client.post(f"/api/exams/reports/{report['id']}/acknowledge", headers=doc).status_code == 422


# ---------------------------------------------------------------- 3 审方规则深化


def test_rules_bulk_import(client, admin, setup):
    payload = [
        {
            "drug_code": "DPX01",
            "max_daily_dose": 100,
            "contraindicated_diagnoses": "消化性溃疡,出血",
            "special_groups": "child,elderly",
        },
        {"drug_code": "DPX02", "max_daily_dose": 50},
    ]
    assert client.post("/api/prescriptions/rules/import", json=payload, headers=setup["doctor"]).status_code == 403
    first = client.post("/api/prescriptions/rules/import", json=payload, headers=admin)
    assert first.status_code == 200 and first.json() == {"imported": 2, "updated": 0}

    # 再次导入 → 按 drug_code 整条更新
    payload[0]["special_groups"] = "child"
    again = client.post("/api/prescriptions/rules/import", json=payload, headers=admin)
    assert again.json() == {"imported": 0, "updated": 2}
    rules = {r["drug_code"]: r for r in client.get("/api/prescriptions/rules", headers=admin).json()}
    assert rules["DPX01"]["special_groups"] == "child"
    assert rules["DPX01"]["contraindicated_diagnoses"] == "消化性溃疡,出血"


def _rx(client, headers, patient_id, org_id, items, diagnosis=""):
    return client.post(
        "/api/prescriptions",
        json={"patient_id": patient_id, "org_id": org_id, "diagnosis_name": diagnosis, "items": items},
        headers=headers,
    ).json()


def test_duplicate_drug_forces_review(client, setup):
    rx = _rx(
        client,
        setup["doctor"],
        setup["adult"]["id"],
        setup["org"]["id"],
        [
            {"drug_code": "DUP01", "drug_name": "阿莫西林", "daily_dose": 1},
            {"drug_code": "DUP01", "drug_name": "阿莫西林胶囊", "daily_dose": 2},
        ],
    )
    assert rx["status"] == "pending_review"
    assert "同方重复药品" in rx["review_comment"]


def test_contraindicated_diagnosis_forces_review(client, setup):
    rx = _rx(
        client,
        setup["doctor"],
        setup["adult"]["id"],
        setup["org"]["id"],
        [{"drug_code": "DPX01", "drug_name": "布洛芬", "daily_dose": 10}],
        diagnosis="消化性溃疡伴出血",
    )
    assert rx["status"] == "pending_review"
    assert "禁忌诊断" in rx["review_comment"] and "消化性溃疡" in rx["review_comment"]

    # 无禁忌诊断的成人使用同药 → 系统审通过
    ok = _rx(
        client,
        setup["doctor"],
        setup["adult"]["id"],
        setup["org"]["id"],
        [{"drug_code": "DPX01", "drug_name": "布洛芬", "daily_dose": 10}],
        diagnosis="偏头痛",
    )
    assert ok["status"] == "auto_passed"


def test_special_groups_force_review(client, admin, setup):
    # 儿童命中 special_groups=child → 转药师审
    child_rx = _rx(
        client,
        setup["doctor"],
        setup["child"]["id"],
        setup["org"]["id"],
        [{"drug_code": "DPX01", "drug_name": "布洛芬", "daily_dose": 10}],
        diagnosis="发热",
    )
    assert child_rx["status"] == "pending_review"
    assert "特殊人群" in child_rx["review_comment"] and "儿童" in child_rx["review_comment"]

    # 老人当前规则 special_groups 仅 child → 通过；恢复 elderly 后 → 转审
    ok = _rx(
        client,
        setup["doctor"],
        setup["elderly"]["id"],
        setup["org"]["id"],
        [{"drug_code": "DPX01", "drug_name": "布洛芬", "daily_dose": 10}],
        diagnosis="关节痛",
    )
    assert ok["status"] == "auto_passed"
    client.post(
        "/api/prescriptions/rules/import",
        json=[
            {
                "drug_code": "DPX01",
                "max_daily_dose": 100,
                "contraindicated_diagnoses": "消化性溃疡,出血",
                "special_groups": "child,elderly",
            }
        ],
        headers=admin,
    )
    elderly_rx = _rx(
        client,
        setup["doctor"],
        setup["elderly"]["id"],
        setup["org"]["id"],
        [{"drug_code": "DPX01", "drug_name": "布洛芬", "daily_dose": 10}],
        diagnosis="关节痛",
    )
    assert elderly_rx["status"] == "pending_review"
    assert "老年人" in elderly_rx["review_comment"]
