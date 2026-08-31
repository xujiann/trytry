"""M9 质量安全：不良事件闭环、病历质控评分、院感上报核实。"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def setup(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "质量测试医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "质量患者", "id_card": "320981199001018001"},
        headers=admin,
    ).json()
    users = {}
    for username, role in [
        ("q_doc", "doctor"),
        ("q_dir", "director"),
        ("q_op", "operator"),
        ("q_ph", "public_health"),
    ]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
        users[role] = login(client, username, "pass123456")
    encounter = client.post(
        "/api/encounters",
        json={"patient_id": patient["id"], "org_id": org["id"], "diagnosis_name": "感冒"},
        headers=users["doctor"],
    ).json()
    return {"org": org, "patient": patient, "encounter": encounter, **users}


def test_adverse_event_closed_loop(client, admin, setup):
    # 实名上报（医师）
    event = client.post(
        "/api/quality/adverse-events",
        json={
            "org_id": setup["org"]["id"],
            "event_type": "medication",
            "level": "II",
            "description": "给药剂量错误，已及时纠正",
        },
        headers=setup["doctor"],
    )
    assert event.status_code == 201, event.text
    body = event.json()
    assert body["reporter_name"] != ""
    eid = body["id"]

    # 未审核不可整改
    assert client.post(
        f"/api/quality/adverse-events/{eid}/rectify",
        json={"note": "过早"},
        headers=setup["operator"],
    ).status_code == 409
    # 审核限管理层
    assert client.post(
        f"/api/quality/adverse-events/{eid}/review",
        json={"note": "属实"},
        headers=setup["doctor"],
    ).status_code == 403
    reviewed = client.post(
        f"/api/quality/adverse-events/{eid}/review",
        json={"note": "情况属实，要求科室整改"},
        headers=setup["director"],
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "reviewed"
    assert reviewed.json()["reviewed_by"] != ""

    rectified = client.post(
        f"/api/quality/adverse-events/{eid}/rectify",
        json={"note": "已开展双人核对培训并修订流程"},
        headers=setup["operator"],
    )
    assert rectified.status_code == 200
    assert rectified.json()["status"] == "rectified"
    assert rectified.json()["rectify_note"] != ""


def test_adverse_event_anonymous_and_stats(client, admin, setup):
    anon = client.post(
        "/api/quality/adverse-events",
        json={
            "org_id": setup["org"]["id"],
            "event_type": "fall",
            "level": "III",
            "description": "患者如厕滑倒，未受伤",
            "anonymous": True,
        },
        headers=setup["public_health"],
    ).json()
    assert anon["anonymous"] is True
    assert anon["reporter_name"] == ""

    stats = client.get("/api/quality/adverse-events-stats", headers=admin).json()
    assert stats["total"] == 2
    assert stats["rectified"] == 1
    assert stats["closed_loop_pct"] == 50.0
    assert stats["by_type"]["medication"] == 1
    assert stats["by_level"]["III"] == 1


def test_record_qc_scoring_and_grades(client, admin, setup):
    # 抽检对象须存在
    missing = client.post(
        "/api/quality/record-qc",
        json={"target_type": "encounter", "target_id": 99999, "score": 90},
        headers=setup["director"],
    )
    assert missing.status_code == 404
    # 经办无权质控
    assert client.post(
        "/api/quality/record-qc",
        json={"target_type": "encounter", "target_id": setup["encounter"]["id"], "score": 95},
        headers=setup["operator"],
    ).status_code == 403

    qc1 = client.post(
        "/api/quality/record-qc",
        json={"target_type": "encounter", "target_id": setup["encounter"]["id"], "score": 95},
        headers=setup["director"],
    ).json()
    assert qc1["grade"] == "甲"
    qc2 = client.post(
        "/api/quality/record-qc",
        json={
            "target_type": "encounter",
            "target_id": setup["encounter"]["id"],
            "score": 72,
            "defects": "主诉缺失;现病史不完整",
        },
        headers=setup["doctor"],
    ).json()
    assert qc2["grade"] == "丙"

    stats = client.get("/api/quality/record-qc-stats", headers=admin).json()
    assert stats["total"] == 2
    assert stats["avg_score"] == round((95 + 72) / 2, 1)
    assert stats["grade_a_pct"] == 50.0
    assert stats["with_defects"] == 1

    listed = client.get("/api/quality/record-qc?grade=丙", headers=admin).json()
    assert len(listed) == 1
    assert "主诉缺失" in listed[0]["defects"]


def test_infection_report_and_verify(client, admin, setup):
    report = client.post(
        "/api/quality/infection-reports",
        json={
            "org_id": setup["org"]["id"],
            "patient_id": setup["patient"]["id"],
            "infection_site": "surgical_site",
            "pathogen": "金黄色葡萄球菌",
            "report_date": "2026-08-10",
        },
        headers=setup["doctor"],
    )
    assert report.status_code == 201, report.text
    rid = report.json()["id"]
    # 经办无权上报院感
    assert client.post(
        "/api/quality/infection-reports",
        json={
            "org_id": setup["org"]["id"],
            "patient_id": setup["patient"]["id"],
            "infection_site": "urinary",
        },
        headers=setup["operator"],
    ).status_code == 403

    confirmed = client.post(
        f"/api/quality/infection-reports/{rid}/verify?confirmed=true",
        headers=setup["public_health"],
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    again = client.post(
        f"/api/quality/infection-reports/{rid}/verify?confirmed=false",
        headers=setup["public_health"],
    )
    assert again.status_code == 409

    stats = client.get("/api/quality/infection-stats", headers=admin).json()
    assert stats["confirmed"] == 1
    assert stats["by_site"]["surgical_site"] == 1
    assert stats["pending_verify"] == 0
