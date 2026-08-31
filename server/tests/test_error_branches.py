"""T6.6：早期模块的异常分支与筛选分支补测。

这批模块（转诊、会诊、家医签约、消毒供应、医废、体检）是第一阶段写的，
主流程一直有测试，但"对象不存在""状态机拒绝""列表筛选"这些分支没覆盖过——
恰恰是改动时最容易悄悄改坏的地方。
"""
import pytest


MISSING_ID = 99999


@pytest.fixture(scope="module")
def base(client, admin):
    county = client.post(
        "/api/organizations", json={"name": "分支演示县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    town = client.post(
        "/api/organizations", json={"name": "分支演示卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients", json={"name": "分支患者", "id_card": "331582199001011234"}, headers=admin
    ).json()
    return {"county": county, "town": town, "patient": patient}


# ---------------------------------------------------------------- 双向转诊


def test_referral_rejects_missing_patient_and_org(client, admin, base):
    assert client.post(
        "/api/referrals",
        json={"patient_id": MISSING_ID, "from_org_id": base["town"]["id"],
              "to_org_id": base["county"]["id"], "direction": "up", "reason": "x"},
        headers=admin,
    ).status_code == 404
    resp = client.post(
        "/api/referrals",
        json={"patient_id": base["patient"]["id"], "from_org_id": MISSING_ID,
              "to_org_id": base["county"]["id"], "direction": "up", "reason": "x"},
        headers=admin,
    )
    assert resp.status_code == 404 and "转出" in resp.json()["detail"]


def test_referral_rejects_same_org(client, admin, base):
    resp = client.post(
        "/api/referrals",
        json={"patient_id": base["patient"]["id"], "from_org_id": base["county"]["id"],
              "to_org_id": base["county"]["id"], "direction": "up", "reason": "x"},
        headers=admin,
    )
    assert resp.status_code == 422


def test_referral_status_filter_and_missing(client, admin, base):
    client.post(
        "/api/referrals",
        json={"patient_id": base["patient"]["id"], "from_org_id": base["town"]["id"],
              "to_org_id": base["county"]["id"], "direction": "up", "reason": "血压控制不佳"},
        headers=admin,
    )
    rows = client.get("/api/referrals?status=pending", headers=admin).json()
    assert rows and all(r["status"] == "pending" for r in rows)
    assert not client.get("/api/referrals?status=completed", headers=admin).json()
    assert client.patch(
        f"/api/referrals/{MISSING_ID}/status", json={"status": "accepted"}, headers=admin
    ).status_code == 404


# ---------------------------------------------------------------- 远程会诊


def test_consultation_validation_branches(client, admin, base):
    assert client.post(
        "/api/consultations",
        json={"patient_id": MISSING_ID, "from_org_id": base["town"]["id"],
              "to_org_id": base["county"]["id"], "question": "x"},
        headers=admin,
    ).status_code == 404
    resp = client.post(
        "/api/consultations",
        json={"patient_id": base["patient"]["id"], "from_org_id": base["town"]["id"],
              "to_org_id": MISSING_ID, "question": "x"},
        headers=admin,
    )
    assert resp.status_code == 404 and "受邀" in resp.json()["detail"]
    assert client.post(
        "/api/consultations",
        json={"patient_id": base["patient"]["id"], "from_org_id": base["town"]["id"],
              "to_org_id": base["town"]["id"], "question": "x"},
        headers=admin,
    ).status_code == 422


def test_consultation_decline_and_state_machine(client, admin, base):
    cons = client.post(
        "/api/consultations",
        json={"patient_id": base["patient"]["id"], "from_org_id": base["town"]["id"],
              "to_org_id": base["county"]["id"], "question": "请求心内科会诊"},
        headers=admin,
    ).json()
    assert client.post(
        f"/api/consultations/{cons['id']}/decline", headers=admin
    ).json()["status"] == "declined"
    # 已拒绝的不可再受理，也不可再拒绝
    assert client.post(
        f"/api/consultations/{cons['id']}/accept", json={"expert_name": "张主任"}, headers=admin
    ).status_code == 409
    assert client.post(f"/api/consultations/{cons['id']}/decline", headers=admin).status_code == 409


def test_consultation_rating_requires_completed(client, admin, base):
    cons = client.post(
        "/api/consultations",
        json={"patient_id": base["patient"]["id"], "from_org_id": base["town"]["id"],
              "to_org_id": base["county"]["id"], "question": "待完成会诊"},
        headers=admin,
    ).json()
    assert client.post(
        f"/api/consultations/{cons['id']}/rate", json={"rating": 5}, headers=admin
    ).status_code == 409


def test_consultation_filter_and_missing(client, admin):
    rows = client.get("/api/consultations?status=declined", headers=admin).json()
    assert rows and all(r["status"] == "declined" for r in rows)
    assert client.post(
        f"/api/consultations/{MISSING_ID}/accept", json={"expert_name": "x"}, headers=admin
    ).status_code == 404


# ---------------------------------------------------------------- 家医签约


def test_contract_validation_and_filters(client, admin, base):
    assert client.post(
        "/api/contracts",
        json={"patient_id": MISSING_ID, "org_id": base["town"]["id"], "doctor_name": "李家医"},
        headers=admin,
    ).status_code == 404
    assert client.post(
        "/api/contracts",
        json={"patient_id": base["patient"]["id"], "org_id": MISSING_ID, "doctor_name": "李家医"},
        headers=admin,
    ).status_code == 404

    contract = client.post(
        "/api/contracts",
        json={"patient_id": base["patient"]["id"], "org_id": base["town"]["id"],
              "doctor_name": "李家医", "package": "standard", "signed_date": "2026-01-01"},
        headers=admin,
    ).json()
    by_org = client.get(f"/api/contracts?org_id={base['town']['id']}", headers=admin).json()
    assert any(x["id"] == contract["id"] for x in by_org)
    by_patient = client.get(
        f"/api/contracts?patient_id={base['patient']['id']}", headers=admin
    ).json()
    assert any(x["id"] == contract["id"] for x in by_patient)
    assert not client.get(f"/api/contracts?org_id={MISSING_ID}", headers=admin).json()


def test_contract_terminate_twice_rejected(client, admin, base):
    contract = client.get(
        f"/api/contracts?patient_id={base['patient']['id']}", headers=admin
    ).json()[0]
    assert client.post(f"/api/contracts/{contract['id']}/terminate", headers=admin).status_code == 200
    assert client.post(f"/api/contracts/{contract['id']}/terminate", headers=admin).status_code == 409
    assert client.post(f"/api/contracts/{MISSING_ID}/terminate", headers=admin).status_code == 404


def test_contract_services_missing_contract(client, admin):
    assert client.post(
        f"/api/contracts/{MISSING_ID}/services", json={"service_type": "visit"}, headers=admin
    ).status_code == 404
    assert client.get(f"/api/contracts/{MISSING_ID}/services", headers=admin).status_code == 404


# ---------------------------------------------------------------- 消毒供应


def test_cssd_validation_and_filters(client, admin, base):
    assert client.post(
        "/api/cssd/batches",
        json={"batch_no": "CS-X1", "center_org_id": MISSING_ID, "item_name": "器械包", "quantity": 5},
        headers=admin,
    ).status_code == 404

    batch = client.post(
        "/api/cssd/batches",
        json={"batch_no": "CS-BR1", "center_org_id": base["county"]["id"],
              "item_name": "手术器械包", "quantity": 10},
        headers=admin,
    ).json()
    assert client.post(
        "/api/cssd/batches",
        json={"batch_no": "CS-BR1", "center_org_id": base["county"]["id"],
              "item_name": "重复批号", "quantity": 1},
        headers=admin,
    ).status_code == 409

    assert client.get("/api/cssd/batches?batch_no=CS-BR1", headers=admin).json()[0]["id"] == batch["id"]
    assert client.get("/api/cssd/batches?status=sterilizing", headers=admin).json()
    assert client.post(f"/api/cssd/batches/{MISSING_ID}/advance", headers=admin).status_code == 404


def test_cssd_dispatch_to_missing_org(client, admin, base):
    batch = client.post(
        "/api/cssd/batches",
        json={"batch_no": "CS-BR2", "center_org_id": base["county"]["id"],
              "item_name": "敷料包", "quantity": 4},
        headers=admin,
    ).json()
    client.post(f"/api/cssd/batches/{batch['id']}/advance", headers=admin)  # → 灭菌
    resp = client.post(
        f"/api/cssd/batches/{batch['id']}/advance?dispatched_to_org_id={MISSING_ID}", headers=admin
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------- 医废追溯


def test_medwaste_validation_filters_and_handover(client, admin, base):
    assert client.post(
        "/api/medwaste",
        json={"org_id": MISSING_ID, "waste_type": "infectious", "weight_kg": 1,
              "collected_date": "2026-08-01"},
        headers=admin,
    ).status_code == 404

    waste = client.post(
        "/api/medwaste",
        json={"org_id": base["town"]["id"], "waste_type": "sharp", "weight_kg": 2.5,
              "collected_date": "2026-08-01"},
        headers=admin,
    ).json()
    assert client.get(f"/api/medwaste?org_id={base['town']['id']}", headers=admin).json()
    assert client.get("/api/medwaste?status=collected", headers=admin).json()
    assert not client.get(f"/api/medwaste?org_id={MISSING_ID}", headers=admin).json()

    handover = {"handler_name": "转运员老张"}
    assert client.post(
        f"/api/medwaste/{MISSING_ID}/handover", json=handover, headers=admin
    ).status_code == 404
    assert client.post(
        f"/api/medwaste/{waste['id']}/handover", json=handover, headers=admin
    ).status_code == 200
    # 已交接不可重复交接
    assert client.post(
        f"/api/medwaste/{waste['id']}/handover", json=handover, headers=admin
    ).status_code == 409


# ---------------------------------------------------------------- 成人体检


def test_checkup_validation_and_filters(client, admin, base):
    assert client.post(
        "/api/checkups",
        json={"patient_id": MISSING_ID, "org_id": base["town"]["id"], "package": "基础套餐",
              "exam_date": "2026-08-01"},
        headers=admin,
    ).status_code == 404
    assert client.post(
        "/api/checkups",
        json={"patient_id": base["patient"]["id"], "org_id": MISSING_ID, "package": "基础套餐",
              "exam_date": "2026-08-01"},
        headers=admin,
    ).status_code == 404

    client.post(
        "/api/checkups",
        json={"patient_id": base["patient"]["id"], "org_id": base["town"]["id"],
              "package": "基础套餐", "exam_date": "2026-08-01",
              "abnormal_items": "空腹血糖偏高"},
        headers=admin,
    )
    client.post(
        "/api/checkups",
        json={"patient_id": base["patient"]["id"], "org_id": base["town"]["id"],
              "package": "基础套餐", "exam_date": "2026-08-02"},
        headers=admin,
    )
    by_patient = client.get(
        f"/api/checkups?patient_id={base['patient']['id']}", headers=admin
    ).json()
    assert len(by_patient) >= 2
    abnormal = client.get("/api/checkups?has_abnormal=true", headers=admin).json()
    assert abnormal and all(x["has_abnormal"] for x in abnormal)
    normal = client.get("/api/checkups?has_abnormal=false", headers=admin).json()
    assert all(not x["has_abnormal"] for x in normal)
