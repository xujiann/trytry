"""终审轮批1：分娩记录、新生儿疾病筛查与高危儿、妇女保健、法定医学证明、成人体检。"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def setup(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "终审妇幼医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "终审孕产妇", "id_card": "330281199202024001", "gender": "女"},
        headers=admin,
    ).json()
    users = {}
    for username, role in [("fg1_doc", "doctor"), ("fg1_ph", "public_health"), ("fg1_op", "operator")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
        users[role] = login(client, username, "pass123456")
    return {"org": org, "patient": patient, **users}


def test_delivery_record_flow(client, setup):
    doc = setup["doctor"]
    record = client.post(
        "/api/maternal/records",
        json={"patient_id": setup["patient"]["id"], "lmp": "2026-01-01", "edc": "2026-10-08"},
        headers=doc,
    ).json()
    # 分娩登记（限医师）：公卫人员 403
    assert (
        client.post(
            f"/api/maternal/records/{record['id']}/delivery",
            json={"org_id": setup["org"]["id"], "delivery_date": "2026-10-05"},
            headers=setup["public_health"],
        ).status_code
        == 403
    )
    resp = client.post(
        f"/api/maternal/records/{record['id']}/delivery",
        json={
            "org_id": setup["org"]["id"],
            "delivery_date": "2026-10-05",
            "delivery_mode": "cesarean",
            "outcome": "母子平安",
        },
        headers=doc,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "delivered"
    # 重复分娩登记 409
    assert (
        client.post(
            f"/api/maternal/records/{record['id']}/delivery",
            json={"org_id": setup["org"]["id"], "delivery_date": "2026-10-06"},
            headers=doc,
        ).status_code
        == 409
    )
    detail = client.get(f"/api/maternal/records/{record['id']}/delivery", headers=doc)
    assert detail.status_code == 200
    assert detail.json()["delivery_mode"] == "cesarean"


def test_newborn_screening_marks_high_risk(client, setup):
    ph = setup["public_health"]
    child = client.post(
        "/api/maternal/children",
        json={"name": "终审新生儿", "birth_date": "2026-10-05"},
        headers=ph,
    ).json()
    ok = client.post(
        f"/api/maternal/children/{child['id']}/screenings",
        json={"item": "metabolic", "result": "normal", "screen_date": "2026-10-08"},
        headers=ph,
    )
    assert ok.status_code == 201
    assert ok.json()["child_high_risk"] is False
    abnormal = client.post(
        f"/api/maternal/children/{child['id']}/screenings",
        json={"item": "hearing", "result": "abnormal", "screen_date": "2026-10-08"},
        headers=ph,
    )
    assert abnormal.status_code == 201
    assert abnormal.json()["child_high_risk"] is True
    # 高危儿清单包含该儿童并注明原因
    hr = client.get("/api/maternal/children/high-risk", headers=ph).json()
    match = [c for c in hr if c["id"] == child["id"]]
    assert match and "听力筛查" in match[0]["risk_note"]
    # 筛查史查询
    screenings = client.get(f"/api/maternal/children/{child['id']}/screenings", headers=ph).json()
    assert len(screenings) == 2
    # 人工解除高危
    off = client.post(
        f"/api/maternal/children/{child['id']}/high-risk",
        json={"high_risk": False},
        headers=setup["doctor"],
    )
    assert off.status_code == 200 and off.json()["high_risk"] is False


def test_women_health_records(client, setup):
    ph = setup["public_health"]
    for record_type in ["premarital", "preconception", "gynecology", "contraception"]:
        resp = client.post(
            "/api/maternal/women-health",
            json={
                "patient_id": setup["patient"]["id"],
                "record_type": record_type,
                "exam_date": "2026-05-01",
                "result": "未见异常",
            },
            headers=ph,
        )
        assert resp.status_code == 201, resp.text
    listed = client.get(
        f"/api/maternal/women-health?patient_id={setup['patient']['id']}&record_type=premarital",
        headers=ph,
    ).json()
    assert len(listed) == 1 and listed[0]["record_type"] == "premarital"
    # 非法类型 422
    assert (
        client.post(
            "/api/maternal/women-health",
            json={"patient_id": setup["patient"]["id"], "record_type": "unknown"},
            headers=ph,
        ).status_code
        == 422
    )


def test_medical_certs(client, setup):
    doc = setup["doctor"]
    birth = client.post(
        "/api/certs",
        json={
            "cert_type": "birth",
            "name": "终审新生儿",
            "gender": "男",
            "event_date": "2026-10-05",
            "org_id": setup["org"]["id"],
        },
        headers=doc,
    )
    assert birth.status_code == 201, birth.text
    assert birth.json()["cert_no"].startswith("B")
    # 死亡证明必须关联患者且填死因
    assert (
        client.post(
            "/api/certs",
            json={
                "cert_type": "death",
                "name": "某人",
                "event_date": "2026-07-01",
                "org_id": setup["org"]["id"],
            },
            headers=doc,
        ).status_code
        == 422
    )
    death = client.post(
        "/api/certs",
        json={
            "cert_type": "death",
            "name": setup["patient"]["name"],
            "event_date": "2026-07-01",
            "detail": "呼吸循环衰竭",
            "org_id": setup["org"]["id"],
            "patient_id": setup["patient"]["id"],
        },
        headers=doc,
    )
    assert death.status_code == 201
    assert death.json()["cert_no"].startswith("D")
    defect = client.post(
        "/api/certs",
        json={
            "cert_type": "defect",
            "name": "终审新生儿",
            "event_date": "2026-10-05",
            "detail": "先天性听力障碍可疑",
            "org_id": setup["org"]["id"],
        },
        headers=setup["public_health"],
    )
    assert defect.status_code == 201
    # 经办人员不可签发
    assert (
        client.post(
            "/api/certs",
            json={
                "cert_type": "birth",
                "name": "越权儿",
                "event_date": "2026-10-05",
                "org_id": setup["org"]["id"],
            },
            headers=setup["operator"],
        ).status_code
        == 403
    )
    stats = client.get("/api/certs/stats", headers=doc).json()
    assert stats["birth"] == 1 and stats["death"] == 1 and stats["defect"] == 1
    listed = client.get("/api/certs?cert_type=death", headers=doc).json()
    assert len(listed) == 1 and listed[0]["detail"] == "呼吸循环衰竭"


def test_physical_exam_and_360(client, setup):
    ph = setup["public_health"]
    normal = client.post(
        "/api/checkups",
        json={
            "patient_id": setup["patient"]["id"],
            "org_id": setup["org"]["id"],
            "exam_date": "2026-06-01",
            "summary": "各项指标正常",
        },
        headers=ph,
    )
    assert normal.status_code == 201
    assert normal.json()["has_abnormal"] is False
    abnormal = client.post(
        "/api/checkups",
        json={
            "patient_id": setup["patient"]["id"],
            "org_id": setup["org"]["id"],
            "package_name": "职工体检套餐",
            "exam_date": "2026-07-01",
            "summary": "血压偏高",
            "abnormal_items": "血压 150/95；空腹血糖 6.8",
        },
        headers=ph,
    )
    assert abnormal.status_code == 201
    assert abnormal.json()["has_abnormal"] is True
    # 异常清单
    ab_list = client.get("/api/checkups/abnormal", headers=ph).json()
    assert any(e["patient_id"] == setup["patient"]["id"] for e in ab_list)
    # 360 档案纳入体检记录
    archive = client.get(f"/api/archive/{setup['patient']['ehc_no']}", headers=ph).json()
    assert len(archive["physical_exams"]) == 2
    assert any(e["has_abnormal"] for e in archive["physical_exams"])
