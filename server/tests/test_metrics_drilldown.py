"""块2：驾驶舱指标下钻——明细条数与 overview/alerts 计数逐项一致（口径不漂移）。"""
from datetime import date, timedelta

import pytest

from conftest import login


@pytest.fixture(scope="module")
def seeded(client, admin):
    """铺一套覆盖全部可下钻指标的业务数据。"""
    county = client.post(
        "/api/organizations",
        json={"name": "下钻县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    township = client.post(
        "/api/organizations",
        json={"name": "下钻乡卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    accounts = {}
    for username, role in [
        ("dd_doc", "doctor"),
        ("dd_ph", "public_health"),
        ("dd_op", "operator"),
        ("dd_pharm", "pharmacist"),
    ]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": county["id"]},
            headers=admin,
        )
        accounts[role] = login(client, username, "pass123456")
    doctor, operator, public_health = accounts["doctor"], accounts["operator"], accounts["public_health"]
    patients = [
        client.post(
            "/api/patients",
            json={"name": f"下钻患者{i}", "id_card": f"33028119900101{i:04d}"},
            headers=admin,
        ).json()
        for i in range(1, 4)
    ]
    # 基层诊疗人次（乡级机构 2 条 + 县级 1 条，只有乡级计入基层口径）
    for org_id, count in [(township["id"], 2), (county["id"], 1)]:
        for _ in range(count):
            client.post(
                "/api/encounters",
                json={
                    "patient_id": patients[0]["id"],
                    "org_id": org_id,
                    "diagnosis_name": "上呼吸道感染",
                    "diagnosis_code": "J06.900",
                },
                headers=doctor,
            )
    # 危急值报告 2 条（其中 1 条处置闭环后不计入未闭环口径）
    report_ids = []
    for i in range(3):
        req = client.post(
            "/api/exams",
            json={
                "patient_id": patients[1]["id"],
                "from_org_id": township["id"],
                "center_type": "imaging",
                "item_code": f"CT-{i}",
                "item_name": f"下钻检查{i}",
            },
            headers=doctor,
        ).json()
        client.post(f"/api/exams/{req['id']}/claim", headers=doctor)
        rep = client.post(
            f"/api/exams/{req['id']}/report",
            json={"conclusion": f"下钻结论{i}", "critical": i < 2},
            headers=doctor,
        ).json()
        report_ids.append(rep["id"])
    client.post(f"/api/exams/reports/{report_ids[0]}/acknowledge", headers=doctor)
    client.post(
        f"/api/exams/reports/{report_ids[0]}/resolve",
        json={"note": "已处置"},
        headers=doctor,
    )
    # 转诊：上转 2 下转 1
    for direction, count in [("up", 2), ("down", 1)]:
        for _ in range(count):
            client.post(
                "/api/referrals",
                json={
                    "patient_id": patients[0]["id"],
                    "from_org_id": township["id"],
                    "to_org_id": county["id"],
                    "direction": direction,
                    "reason": "下钻测试",
                },
                headers=doctor,
            )
    # 缺药预警：库存低于阈值
    client.post(
        "/api/pharmacy/stocks",
        json={
            "org_id": county["id"],
            "drug_code": "DD-LOW",
            "drug_name": "下钻短缺药",
            "quantity": 1,
            "threshold": 50,
        },
        headers=admin,
    )
    client.post(
        "/api/pharmacy/stocks",
        json={
            "org_id": county["id"],
            "drug_code": "DD-OK",
            "drug_name": "下钻充足药",
            "quantity": 500,
            "threshold": 50,
        },
        headers=admin,
    )
    # 慢病随访超期
    chronic = client.post(
        "/api/chronic",
        json={
            "patient_id": patients[2]["id"],
            "disease": "hypertension",
            "level": 3,
            "managed_by_org_id": township["id"],
        },
        headers=public_health,
    )
    assert chronic.status_code in (200, 201), chronic.text
    # 医废滞留：收集日期早于 2 天前且未交接
    client.post(
        "/api/medwaste",
        json={
            "org_id": county["id"],
            "waste_type": "infectious",
            "weight_kg": 3.5,
            "collected_date": (date.today() - timedelta(days=5)).isoformat(),
        },
        headers=operator,
    )
    # 近 7 日传染病个案
    client.post(
        "/api/infectious/cases",
        json={
            "org_id": county["id"],
            "disease_code": "A01",
            "disease_name": "伤寒",
            "onset_date": (date.today() - timedelta(days=1)).isoformat(),
        },
        headers=public_health,
    )
    # 处方：待药师审（超剂量触发）+ 退回
    client.post(
        "/api/prescriptions/rules",
        json={"drug_code": "DD-RX", "max_daily_dose": 10, "dose_unit": "mg"},
        headers=admin,
    )
    rx_ids = []
    for _ in range(2):
        rx = client.post(
            "/api/prescriptions",
            json={
                "patient_id": patients[0]["id"],
                "org_id": county["id"],
                "diagnosis_name": "下钻诊断",
                "items": [
                    {"drug_code": "DD-RX", "drug_name": "下钻药", "daily_dose": 99, "days": 3}
                ],
            },
            headers=doctor,
        ).json()
        rx_ids.append(rx["id"])
    client.post(
        f"/api/prescriptions/{rx_ids[0]}/review",
        json={"approve": False, "comment": "剂量过大"},
        headers=accounts["pharmacist"],
    )
    return {"county": county, "township": township, "patients": patients, "accounts": accounts}


ALL_METRICS = [
    "critical_values",
    "stock_alerts",
    "chronic_overdue",
    "medwaste_overdue",
    "infectious_recent",
    "referrals_up",
    "referrals_down",
    "referrals_completed",
    "grassroots_encounters",
    "reported_exams",
    "recognized_exams",
    "pending_reviews",
    "rejected_prescriptions",
]


def _drill(client, admin, metric, params=""):
    resp = client.get(f"/api/metrics/drilldown?metric={metric}{params}", headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_drilldown_total_matches_overview(client, admin, seeded):
    """关键断言：每个指标的明细总数与 /overview 对应口径完全一致。"""
    ov = client.get("/api/metrics/overview", headers=admin).json()
    expected = {
        "critical_values": ov["remote_diagnosis"]["critical_values"],
        "stock_alerts": ov["pharmacy"]["stock_alerts"],
        "referrals_up": ov["referrals"]["up"],
        "referrals_down": ov["referrals"]["down"],
        "referrals_completed": ov["referrals"]["completed"],
        "grassroots_encounters": ov["service_division"]["grassroots_encounters"],
        "reported_exams": ov["remote_diagnosis"]["reported_total"],
        "recognized_exams": ov["remote_diagnosis"]["recognized_total"],
        "pending_reviews": ov["prescription_review"]["pending_review"],
        "rejected_prescriptions": ov["prescription_review"]["rejected"],
    }
    for metric, count in expected.items():
        data = _drill(client, admin, metric)
        assert data["total"] == count, f"{metric} 明细总数与 overview 不一致"
        assert len(data["items"]) == count, f"{metric} 明细行数与 overview 不一致"


def test_drilldown_total_matches_alerts(client, admin, seeded):
    """关键断言：预警横幅各类计数与下钻明细条数完全一致。"""
    alerts = client.get("/api/metrics/alerts", headers=admin).json()
    counts = {a["type"]: a["count"] for a in alerts["items"]}
    assert counts, "预警横幅应至少有一类风险"
    for metric, count in counts.items():
        data = _drill(client, admin, metric)
        assert data["total"] == count, f"{metric} 与 alerts 计数不一致"
        assert len(data["items"]) == count


def test_every_metric_drillable_and_matches_catalog(client, admin, seeded):
    catalog = {m["metric"]: m["count"] for m in client.get("/api/metrics/drilldown-metrics", headers=admin).json()}
    assert set(catalog) == set(ALL_METRICS)
    for metric in ALL_METRICS:
        data = _drill(client, admin, metric)
        assert data["total"] == catalog[metric], metric
        assert data["metric"] == metric and data["label"] and data["page"]
        assert len(data["columns"]) == len(data["fields"])
        for row in data["items"]:
            assert "id" in row  # 跳转所需业务id
            assert set(data["fields"]) <= set(row)


def test_seeded_counts_are_nonzero_and_correct(client, admin, seeded):
    """口径正确性抽验：闭环危急值不计入、非基层就诊不计入、库存充足不预警。"""
    assert _drill(client, admin, "critical_values")["total"] == 1  # 2 条危急值中 1 条已处置
    assert _drill(client, admin, "grassroots_encounters")["total"] == 2  # 县级 1 条不计入
    assert _drill(client, admin, "stock_alerts")["total"] == 1
    assert _drill(client, admin, "chronic_overdue")["total"] == 0  # 新建档随访未到期
    assert _drill(client, admin, "medwaste_overdue")["total"] == 1
    assert _drill(client, admin, "infectious_recent")["total"] == 1
    assert _drill(client, admin, "referrals_up")["total"] == 2
    assert _drill(client, admin, "referrals_down")["total"] == 1
    assert _drill(client, admin, "pending_reviews")["total"] == 1  # 2 条待审中 1 条已退回
    assert _drill(client, admin, "rejected_prescriptions")["total"] == 1


def test_drilldown_pagination(client, admin, seeded):
    total = _drill(client, admin, "referrals_up")["total"]
    assert total >= 2
    page = _drill(client, admin, "referrals_up", "&offset=1&limit=1")
    assert page["total"] == total  # 总数不受分页影响
    assert len(page["items"]) == 1
    first = _drill(client, admin, "referrals_up", "&offset=0&limit=1")["items"][0]
    assert page["items"][0]["id"] != first["id"]


def test_unknown_metric_returns_422(client, admin):
    resp = client.get("/api/metrics/drilldown?metric=not_a_metric", headers=admin)
    assert resp.status_code == 422


def test_drilldown_requires_login(client):
    assert client.get("/api/metrics/drilldown?metric=critical_values").status_code == 401


def test_chronic_overdue_drilldown_after_due_date(client, admin, seeded):
    """把随访日改成过去 → overview/alerts/下钻三处同步反映（同一查询构造函数）。"""
    from app.database import SessionLocal
    from app.models import ChronicPatient

    db = SessionLocal()
    try:
        chronic = db.query(ChronicPatient).order_by(ChronicPatient.id.desc()).first()
        chronic.next_due = (date.today() - timedelta(days=10)).isoformat()
        db.commit()
        chronic_id = chronic.id
    finally:
        db.close()
    data = _drill(client, admin, "chronic_overdue")
    assert data["total"] == 1
    assert data["items"][0]["id"] == chronic_id
    alerts = {a["type"]: a["count"] for a in client.get("/api/metrics/alerts", headers=admin).json()["items"]}
    assert alerts["chronic_overdue"] == 1
