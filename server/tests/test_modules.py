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
def headers(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def base_data(client, headers):
    """县医院 + 乡镇卫生院 + 患者，供各模块测试复用。"""
    county = client.post(
        "/api/organizations",
        json={"name": "县人民医院", "org_type": "lead_hospital", "level": "county"},
        headers=headers,
    ).json()
    township = client.post(
        "/api/organizations",
        json={"name": "南镇卫生院", "org_type": "township", "level": "township", "parent_id": county["id"]},
        headers=headers,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "李四", "id_card": "320981198505054321", "gender": "女"},
        headers=headers,
    ).json()
    return {"county": county, "township": township, "patient": patient}


def test_dictionary_bulk_import(client, headers):
    payload = [
        {"code": "TST-BULK1", "name": "批量导入测试条目一"},
        {"code": "TST-BULK2", "name": "批量导入测试条目二"},
        {"code": "TST-BULK1", "name": "重复条目"},
    ]
    resp = client.post("/api/dictionaries/diagnosis/import", json=payload, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"imported": 2, "skipped": 1}


def test_encounter_and_archive(client, headers, base_data):
    resp = client.post(
        "/api/encounters",
        json={
            "patient_id": base_data["patient"]["id"],
            "org_id": base_data["township"]["id"],
            "doctor_name": "王医生",
            "diagnosis_code": "I10",
            "diagnosis_name": "高血压",
            "summary": "血压 150/95，建议规律服药",
        },
        headers=headers,
    )
    assert resp.status_code == 201

    archive = client.get(f"/api/archive/{base_data['patient']['ehc_no']}", headers=headers)
    assert archive.status_code == 200
    assert archive.json()["encounters"][0]["diagnosis_name"] == "高血压"


def test_exam_center_flow_with_recognition(client, headers, base_data):
    pid, org = base_data["patient"]["id"], base_data["township"]["id"]

    # 首次开单：无可互认结果
    check = client.get(
        f"/api/exams/recognition-check?patient_id={pid}&item_code=CT-CHEST", headers=headers
    )
    assert check.json()["recognizable"] is False

    request = client.post(
        "/api/exams",
        json={
            "patient_id": pid,
            "from_org_id": org,
            "center_type": "imaging",
            "item_code": "CT-CHEST",
            "item_name": "胸部CT",
            "clinical_info": "咳嗽两周",
        },
        headers=headers,
    ).json()
    assert request["status"] == "pending"

    # 中心领取并出报告（危急值）
    claimed = client.post(f"/api/exams/{request['id']}/claim", headers=headers)
    assert claimed.json()["status"] == "diagnosing"
    report = client.post(
        f"/api/exams/{request['id']}/report",
        json={"finding": "右肺占位", "conclusion": "考虑占位性病变，建议住院", "critical": True, "reported_by": "县院影像科"},
        headers=headers,
    )
    assert report.status_code == 201

    # 危急值清单可见
    critical = client.get("/api/exams/critical", headers=headers)
    assert len(critical.json()) == 1

    # 再次开同一项目：提示可互认，互认建单不再重复检查
    check2 = client.get(
        f"/api/exams/recognition-check?patient_id={pid}&item_code=CT-CHEST", headers=headers
    ).json()
    assert check2["recognizable"] is True
    recognized = client.post(
        "/api/exams",
        json={
            "patient_id": pid,
            "from_org_id": org,
            "center_type": "imaging",
            "item_code": "CT-CHEST",
            "item_name": "胸部CT",
            "accept_recognition_of": check2["request_id"],
        },
        headers=headers,
    ).json()
    assert recognized["status"] == "recognized"
    assert recognized["recognized_from_id"] == request["id"]


def test_prescription_double_review(client, headers, base_data):
    pid, org = base_data["patient"]["id"], base_data["township"]["id"]
    rule = client.post(
        "/api/prescriptions/rules",
        json={"drug_code": "METFORMIN", "max_daily_dose": 2000, "dose_unit": "mg"},
        headers=headers,
    )
    assert rule.status_code == 201

    # 规则内：系统审自动通过
    ok = client.post(
        "/api/prescriptions",
        json={
            "patient_id": pid,
            "org_id": org,
            "diagnosis_name": "2型糖尿病",
            "items": [{"drug_code": "METFORMIN", "drug_name": "二甲双胍", "daily_dose": 1500, "days": 30}],
        },
        headers=headers,
    ).json()
    assert ok["status"] == "auto_passed"

    # 超量：转入药师审并可退回
    over = client.post(
        "/api/prescriptions",
        json={
            "patient_id": pid,
            "org_id": org,
            "diagnosis_name": "2型糖尿病",
            "items": [{"drug_code": "METFORMIN", "drug_name": "二甲双胍", "daily_dose": 3000, "days": 30}],
        },
        headers=headers,
    ).json()
    assert over["status"] == "pending_review"
    assert "超过上限" in over["review_comment"]

    reviewed = client.post(
        f"/api/prescriptions/{over['id']}/review",
        json={"approve": False, "comment": "剂量过大，请调整"},
        headers=headers,
    ).json()
    assert reviewed["status"] == "rejected"
    assert "药师意见" in reviewed["review_comment"]


def test_pharmacy_transfer_and_alerts(client, headers, base_data):
    county, township = base_data["county"]["id"], base_data["township"]["id"]
    client.post(
        "/api/pharmacy/stocks",
        json={"org_id": county, "drug_code": "METFORMIN", "drug_name": "二甲双胍", "quantity": 100, "threshold": 20},
        headers=headers,
    )

    # 库存不足的调拨被拒绝
    too_much = client.post(
        "/api/pharmacy/transfers",
        json={"drug_code": "METFORMIN", "from_org_id": county, "to_org_id": township, "quantity": 500},
        headers=headers,
    )
    assert too_much.status_code == 409

    dest = client.post(
        "/api/pharmacy/transfers",
        json={"drug_code": "METFORMIN", "from_org_id": county, "to_org_id": township, "quantity": 90},
        headers=headers,
    ).json()
    assert dest["org_id"] == township and dest["quantity"] == 90

    # 调出后县院库存 10 < 阈值 20，触发缺药预警
    alerts = client.get("/api/pharmacy/alerts", headers=headers).json()
    assert any(a["org_id"] == county and a["quantity"] == 10 for a in alerts)


def test_chronic_smart_leveling_and_overdue(client, headers, base_data):
    chronic = client.post(
        "/api/chronic",
        json={
            "patient_id": base_data["patient"]["id"],
            "disease": "hypertension",
            "managed_by_org_id": base_data["township"]["id"],
        },
        headers=headers,
    ).json()
    assert chronic["level"] == 1

    # 高压 165：升为3级并建议上转，返回膳食运动指导要点
    result = client.post(
        f"/api/chronic/{chronic['id']}/followups",
        json={"sbp": 165, "dbp": 95, "next_due": "2026-01-01"},
        headers=headers,
    ).json()
    assert result["level"] == 3
    assert result["refer_up_suggested"] is True
    assert "限盐" in result["guidance_points"]

    # next_due 已过期 → 出现在超期名单
    overdue = client.get("/api/chronic/overdue?today=2026-06-01", headers=headers).json()
    assert [c["id"] for c in overdue] == [chronic["id"]]

    # 控制良好后降为1级
    good = client.post(
        f"/api/chronic/{chronic['id']}/followups",
        json={"sbp": 128, "dbp": 82, "next_due": "2099-01-01"},
        headers=headers,
    ).json()
    assert good["level"] == 1


def test_infectious_multi_point_alert(client, headers, base_data):
    for org_key, days in (("county", 3), ("township", 2)):
        for i in range(days):
            resp = client.post(
                "/api/infectious/cases",
                json={
                    "org_id": base_data[org_key]["id"],
                    "disease_code": "J11",
                    "disease_name": "流行性感冒",
                    "onset_date": f"2026-08-0{i + 1}",
                },
                headers=headers,
            )
            assert resp.status_code == 201

    alerts = client.get(
        "/api/infectious/alerts?window_days=7&threshold=5&today=2026-08-05", headers=headers
    ).json()
    assert len(alerts) == 1
    assert alerts[0]["case_count"] == 5
    assert alerts[0]["severity"] == "high"  # 两家机构报告，多点触发升级

    # 阈值未达到时不预警
    none = client.get(
        "/api/infectious/alerts?window_days=7&threshold=6&today=2026-08-05", headers=headers
    ).json()
    assert none == []


def test_portal_identity_verification(client, headers, base_data, monkeypatch):
    # 旧双因子接口默认已关（portal_legacy_verify=False，A2/P1-3），显式开启后测
    from app.config import settings

    monkeypatch.setattr(settings, "portal_legacy_verify", True)
    patient = base_data["patient"]
    # `chronic_care` 原本靠上一条用例（test_chronic_smart_leveling_and_overdue）
    # 顺带建出来——整模块跑得过是因为它排在前面；`pytest -k portal_identity`
    # 只选中本条时 `chronic_care` 是空的，`[0]` 直接 IndexError。
    # 本条自己保证这份前置数据存在。
    if not client.get(
        f"/api/chronic?patient_id={patient['id']}", headers=headers
    ).json():
        chronic = client.post(
            "/api/chronic",
            json={"patient_id": patient["id"], "disease": "hypertension",
                  "managed_by_org_id": base_data["township"]["id"]},
            headers=headers,
        ).json()
        client.post(
            f"/api/chronic/{chronic['id']}/followups",
            json={"sbp": 165, "dbp": 95, "next_due": "2026-01-01"},
            headers=headers,
        )

    wrong = client.get(f"/api/portal/my-archive?ehc_no={patient['ehc_no']}&id_card=WRONG")
    assert wrong.status_code == 403

    mine = client.get(
        f"/api/portal/my-archive?ehc_no={patient['ehc_no']}&id_card={patient['id_card']}"
    )
    assert mine.status_code == 200
    body = mine.json()
    assert body["name"] == "李四"
    assert body["chronic_care"][0]["guidance_points"] != ""


def test_metrics_overview(client, headers):
    resp = client.get("/api/metrics/overview", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["resources"]["organizations"] == 2
    assert data["service_division"]["grassroots_encounter_ratio_pct"] == 100.0
    assert data["remote_diagnosis"]["reported_total"] == 1
    assert data["remote_diagnosis"]["recognized_total"] == 1
    assert data["remote_diagnosis"]["recognition_ratio_pct"] == 50.0
    assert data["remote_diagnosis"]["critical_values"] == 1
    assert data["prescription_review"]["total"] == 2
    assert data["pharmacy"]["stock_alerts"] == 1


def test_metrics_trends_and_alerts(client, headers):
    trends = client.get("/api/metrics/trends?months=3", headers=headers).json()
    assert len(trends["months"]) == 3
    assert set(trends["series"]) == {"encounters", "exam_reports", "referrals", "prescriptions"}
    # 本模块数据均为当月产生，应计入最后一个月
    assert trends["series"]["encounters"][-1] >= 1

    alerts = client.get("/api/metrics/alerts", headers=headers).json()
    labels = {i["type"]: i["count"] for i in alerts["items"]}
    assert labels.get("critical_values") == 1
    assert labels.get("stock_alerts") == 1
    assert alerts["total"] >= 2


def test_patient_360_view_is_bounded(client, headers, base_data):
    """T6.4：360 视图各段有上限并标出 has_more，与居民端 limit(50) 口径一致。"""
    from app.routers.encounters import ARCHIVE_SECTION_LIMIT

    patient = base_data["patient"]
    org = base_data["county"]
    for i in range(ARCHIVE_SECTION_LIMIT + 3):
        client.post(
            "/api/encounters",
            json={"patient_id": patient["id"], "org_id": org["id"], "doctor_name": "医生",
                  "diagnosis_name": f"复诊{i}"},
            headers=headers,
        )
    view = client.get(f"/api/archive/{patient['ehc_no']}", headers=headers).json()
    assert view["section_limit"] == ARCHIVE_SECTION_LIMIT
    assert len(view["encounters"]) == ARCHIVE_SECTION_LIMIT
    assert view["has_more"]["encounters"] is True
    # 未超限的段不应误报 has_more
    assert view["has_more"]["checkups"] is False
