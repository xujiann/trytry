"""终审轮批2：处方点评、供应商/采购验收/盘点、统一知识库、检查资源、双通道申报、供应风险。"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def setup(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "终审药事医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "终审药事患者", "id_card": "330281199001015002"},
        headers=admin,
    ).json()
    users = {}
    for username, role in [
        ("fg2_doc", "doctor"),
        ("fg2_pha", "pharmacist"),
        ("fg2_dir", "director"),
        ("fg2_op", "operator"),
        ("fg2_ph", "public_health"),
    ]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
        users[role] = login(client, username, "pass123456")
    return {"org": org, "patient": patient, **users}


def test_prescription_comment_review(client, setup):
    rx = client.post(
        "/api/prescriptions",
        json={
            "patient_id": setup["patient"]["id"],
            "org_id": setup["org"]["id"],
            "diagnosis_name": "上呼吸道感染",
            "items": [{"drug_code": "AMX", "drug_name": "阿莫西林", "daily_dose": 1.5, "days": 3}],
        },
        headers=setup["doctor"],
    )
    assert rx.status_code == 201, rx.text
    rx_id = rx.json()["id"]
    # 点评限药师：医师 403
    assert (
        client.post(
            f"/api/prescriptions/{rx_id}/comment-review",
            json={"grade": "reasonable"},
            headers=setup["doctor"],
        ).status_code
        == 403
    )
    # 不合理点评须注明问题
    assert (
        client.post(
            f"/api/prescriptions/{rx_id}/comment-review",
            json={"grade": "unreasonable"},
            headers=setup["pharmacist"],
        ).status_code
        == 422
    )
    ok = client.post(
        f"/api/prescriptions/{rx_id}/comment-review",
        json={"grade": "unreasonable", "issues": "用法用量不适宜", "comment": "剂量偏大"},
        headers=setup["pharmacist"],
    )
    assert ok.status_code == 201
    # 同方重复点评 409
    assert (
        client.post(
            f"/api/prescriptions/{rx_id}/comment-review",
            json={"grade": "reasonable"},
            headers=setup["pharmacist"],
        ).status_code
        == 409
    )
    stats = client.get("/api/prescriptions/comment-stats", headers=setup["pharmacist"]).json()
    assert stats["commented"] == 1 and stats["unreasonable"] == 1
    listed = client.get(
        "/api/prescriptions/comment-reviews?grade=unreasonable", headers=setup["pharmacist"]
    ).json()
    assert len(listed) == 1 and "用法用量" in listed[0]["issues"]


def test_supplier_purchase_receive_flow(client, setup):
    op = setup["operator"]
    supplier = client.post(
        "/api/pharmacy/suppliers",
        json={"name": "县医药公司", "contact": "0574-123456", "license_no": "浙AA1234"},
        headers=setup["director"],
    )
    assert supplier.status_code == 201
    sid = supplier.json()["id"]
    # 重名 409
    assert (
        client.post("/api/pharmacy/suppliers", json={"name": "县医药公司"}, headers=op).status_code
        == 409
    )
    order = client.post(
        "/api/pharmacy/purchase-orders",
        json={
            "org_id": setup["org"]["id"],
            "supplier_id": sid,
            "item_type": "drug",
            "item_code": "MET",
            "item_name": "二甲双胍",
            "quantity": 500,
        },
        headers=op,
    )
    assert order.status_code == 201
    oid = order.json()["id"]
    # 未审批不可验收
    assert client.post(f"/api/pharmacy/purchase-orders/{oid}/receive", headers=op).status_code == 409
    # 审批限管理层
    assert client.post(f"/api/pharmacy/purchase-orders/{oid}/approve", headers=op).status_code == 403
    approved = client.post(
        f"/api/pharmacy/purchase-orders/{oid}/approve", headers=setup["director"]
    )
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    received = client.post(f"/api/pharmacy/purchase-orders/{oid}/receive", headers=op)
    assert received.status_code == 200
    assert received.json()["stock_quantity"] == 500  # 药品验收自动入库
    # 盘点：实盘 480，差异 -20，库存调整为实盘数
    take = client.post(
        "/api/pharmacy/stock-takes",
        json={"org_id": setup["org"]["id"], "drug_code": "MET", "actual_qty": 480, "note": "破损20"},
        headers=setup["pharmacist"],
    )
    assert take.status_code == 201
    assert take.json()["diff"] == -20
    stocks = client.get(f"/api/pharmacy/stocks?org_id={setup['org']['id']}", headers=op).json()
    met = [s for s in stocks if s["drug_code"] == "MET"][0]
    assert met["quantity"] == 480


def test_knowledge_base(client, setup):
    dir_h = setup["director"]
    for category, title in [
        ("drug_policy", "国家基本药物目录（2018年版）使用要求"),
        ("clinical_guideline", "高血压基层诊疗指南"),
        ("referral", "胸痛患者上转指征"),
        ("tcm_health", "秋季养生要点"),
    ]:
        assert (
            client.post(
                "/api/knowledge", json={"category": category, "title": title}, headers=dir_h
            ).status_code
            == 201
        )
    # 质管制度带有效期：已过期条目默认不返回
    expired = client.post(
        "/api/knowledge",
        json={
            "category": "regulation",
            "title": "旧版病历书写制度",
            "expire_date": "2025-12-31",
        },
        headers=setup["public_health"],
    ).json()
    default = client.get("/api/knowledge?category=regulation&today=2026-08-11", headers=dir_h).json()
    assert default == []
    included = client.get(
        "/api/knowledge?category=regulation&include_expired=true&today=2026-08-11", headers=dir_h
    ).json()
    assert len(included) == 1 and included[0]["expired"] is True
    # 检索与临期提醒
    hits = client.get("/api/knowledge?q=高血压", headers=setup["doctor"]).json()
    assert len(hits) == 1 and hits[0]["category"] == "clinical_guideline"
    client.patch(
        f"/api/knowledge/{expired['id']}", json={"expire_date": "2026-08-20"}, headers=dir_h
    )
    expiring = client.get("/api/knowledge/expiring?days=30&today=2026-08-11", headers=dir_h).json()
    assert any(e["id"] == expired["id"] for e in expiring)
    # 经办不可发布
    assert (
        client.post(
            "/api/knowledge",
            json={"category": "referral", "title": "越权"},
            headers=setup["operator"],
        ).status_code
        == 403
    )


def test_exam_resources(client, admin, setup):
    resp = client.post(
        "/api/exams/resources",
        json={
            "org_id": setup["org"]["id"],
            "center_type": "imaging",
            "item_name": "胸部CT平扫",
            "device": "64排螺旋CT",
            "price": 240.0,
            "duration_min": 10,
            "notes": "检查前去除金属物品",
        },
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    # 建档限管理员
    assert (
        client.post(
            "/api/exams/resources",
            json={"org_id": setup["org"]["id"], "center_type": "ecg", "item_name": "常规心电图"},
            headers=setup["doctor"],
        ).status_code
        == 403
    )
    listed = client.get("/api/exams/resources?center_type=imaging", headers=setup["doctor"]).json()
    assert len(listed) == 1 and listed[0]["price"] == 240.0


def test_dual_channel_application(client, setup):
    app_resp = client.post(
        "/api/insurance/dual-channel",
        json={"patient_id": setup["patient"]["id"], "drug_name": "阿达木单抗", "reason": "类风湿"},
        headers=setup["doctor"],
    )
    assert app_resp.status_code == 201
    aid = app_resp.json()["id"]
    # 审核限管理层
    assert (
        client.post(
            f"/api/insurance/dual-channel/{aid}/review?approve=true", headers=setup["doctor"]
        ).status_code
        == 403
    )
    reviewed = client.post(
        f"/api/insurance/dual-channel/{aid}/review?approve=true&comment=符合双通道条件",
        headers=setup["director"],
    )
    assert reviewed.status_code == 200 and reviewed.json()["status"] == "approved"
    # 重复审核 409
    assert (
        client.post(
            f"/api/insurance/dual-channel/{aid}/review?approve=false", headers=setup["director"]
        ).status_code
        == 409
    )
    listed = client.get("/api/insurance/dual-channel?status=approved", headers=setup["operator"]).json()
    assert len(listed) == 1


def test_supply_risk_assessment(client, admin, setup):
    op = setup["operator"]
    # 库存低于阈值 → 中风险
    client.post(
        "/api/pharmacy/stocks",
        json={
            "org_id": setup["org"]["id"],
            "drug_code": "INS",
            "drug_name": "胰岛素",
            "quantity": 5,
            "threshold": 50,
        },
        headers=admin,
    )
    risk = client.get("/api/medication/supply-risk", headers=op).json()
    ins = [r for r in risk["risks"] if r["drug_code"] == "INS"]
    assert ins and ins[0]["risk_level"] == "medium"
    # 叠加未结案缺药登记 → 高风险
    client.post(
        "/api/medication/shortages",
        json={
            "org_id": setup["org"]["id"],
            "drug_code": "INS",
            "drug_name": "胰岛素",
            "quantity": 30,
        },
        headers=op,
    )
    risk = client.get("/api/medication/supply-risk", headers=op).json()
    ins = [r for r in risk["risks"] if r["drug_code"] == "INS"]
    assert ins and ins[0]["risk_level"] == "high" and ins[0]["open_shortages"] == 1
