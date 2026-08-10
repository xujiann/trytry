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
    county = client.post(
        "/api/organizations",
        json={"name": "县人民医院", "org_type": "lead_hospital", "level": "county"},
        headers=headers,
    ).json()
    township = client.post(
        "/api/organizations",
        json={"name": "北镇卫生院", "org_type": "township", "level": "township", "parent_id": county["id"]},
        headers=headers,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "陈军", "id_card": "320981197001019876", "gender": "男"},
        headers=headers,
    ).json()
    return {"county": county, "township": township, "patient": patient}


def test_consultation_full_flow(client, headers, base_data):
    consultation = client.post(
        "/api/consultations",
        json={
            "patient_id": base_data["patient"]["id"],
            "from_org_id": base_data["township"]["id"],
            "to_org_id": base_data["county"]["id"],
            "question": "反复胸闷伴心电图异常，请求心内科会诊",
        },
        headers=headers,
    ).json()
    assert consultation["status"] == "applied"

    # 未受理不能直接出意见
    early = client.post(
        f"/api/consultations/{consultation['id']}/complete",
        json={"opinion": "跳过受理"},
        headers=headers,
    )
    assert early.status_code == 409

    accepted = client.post(
        f"/api/consultations/{consultation['id']}/accept",
        json={"expert_name": "心内科张主任"},
        headers=headers,
    ).json()
    assert accepted["status"] == "accepted"

    completed = client.post(
        f"/api/consultations/{consultation['id']}/complete",
        json={"opinion": "建议完善冠脉CTA，先按不稳定心绞痛处理"},
        headers=headers,
    ).json()
    assert completed["status"] == "completed"

    rated = client.post(
        f"/api/consultations/{consultation['id']}/rate", json={"rating": 5}, headers=headers
    ).json()
    assert rated["rating"] == 5


def test_family_doctor_contract(client, headers, base_data):
    contract = client.post(
        "/api/contracts",
        json={
            "patient_id": base_data["patient"]["id"],
            "org_id": base_data["township"]["id"],
            "doctor_name": "李家医",
            "package": "standard",
            "signed_date": "2026-08-10",
        },
        headers=headers,
    ).json()
    assert contract["status"] == "active"

    dup = client.post(
        "/api/contracts",
        json={
            "patient_id": base_data["patient"]["id"],
            "org_id": base_data["township"]["id"],
            "doctor_name": "李家医",
        },
        headers=headers,
    )
    assert dup.status_code == 409

    service = client.post(
        f"/api/contracts/{contract['id']}/services",
        json={"service_type": "visit", "note": "上门测血压"},
        headers=headers,
    )
    assert service.status_code == 201

    terminated = client.post(f"/api/contracts/{contract['id']}/terminate", headers=headers).json()
    assert terminated["status"] == "terminated"

    # 解约后不可记录履约；重新签约恢复 active
    blocked = client.post(
        f"/api/contracts/{contract['id']}/services",
        json={"service_type": "consult"},
        headers=headers,
    )
    assert blocked.status_code == 409
    resigned = client.post(
        "/api/contracts",
        json={
            "patient_id": base_data["patient"]["id"],
            "org_id": base_data["township"]["id"],
            "doctor_name": "李家医",
            "package": "premium",
        },
        headers=headers,
    ).json()
    assert resigned["id"] == contract["id"]
    assert resigned["status"] == "active" and resigned["package"] == "premium"


def test_appointment_capacity_and_cancel(client, headers, base_data):
    slot = client.post(
        "/api/appointments/slots",
        json={
            "org_id": base_data["county"]["id"],
            "resource_type": "exam",
            "resource_name": "CT室上午",
            "slot_date": "2026-08-20",
            "slot_time": "09:00-10:00",
            "capacity": 1,
        },
        headers=headers,
    ).json()

    appointment = client.post(
        "/api/appointments",
        json={"slot_id": slot["id"], "patient_id": base_data["patient"]["id"]},
        headers=headers,
    ).json()
    assert appointment["status"] == "booked"

    # 容量1已满，其他患者约不进
    other = client.post(
        "/api/patients",
        json={"name": "赵敏", "id_card": "320981199505051111", "gender": "女"},
        headers=headers,
    ).json()
    full = client.post(
        "/api/appointments",
        json={"slot_id": slot["id"], "patient_id": other["id"]},
        headers=headers,
    )
    assert full.status_code == 409

    # 取消释放号源后可再约
    client.post(f"/api/appointments/{appointment['id']}/cancel", headers=headers)
    retry = client.post(
        "/api/appointments",
        json={"slot_id": slot["id"], "patient_id": other["id"]},
        headers=headers,
    )
    assert retry.status_code == 201


def test_cssd_traceability_flow(client, headers, base_data):
    batch = client.post(
        "/api/cssd/batches",
        json={
            "batch_no": "CSSD-20260810-01",
            "center_org_id": base_data["county"]["id"],
            "item_name": "手术器械包",
            "quantity": 20,
        },
        headers=headers,
    ).json()
    assert batch["status"] == "sterilizing"

    sterile = client.post(f"/api/cssd/batches/{batch['id']}/advance", headers=headers).json()
    assert sterile["status"] == "sterile"

    # 发放必须指定接收机构
    no_org = client.post(f"/api/cssd/batches/{batch['id']}/advance", headers=headers)
    assert no_org.status_code == 422
    dispatched = client.post(
        f"/api/cssd/batches/{batch['id']}/advance?dispatched_to_org_id={base_data['township']['id']}",
        headers=headers,
    ).json()
    assert dispatched["status"] == "dispatched"
    assert dispatched["dispatched_to_org_id"] == base_data["township"]["id"]

    recycled = client.post(f"/api/cssd/batches/{batch['id']}/advance", headers=headers).json()
    assert recycled["status"] == "recycled"

    # 终态不可再流转
    done = client.post(f"/api/cssd/batches/{batch['id']}/advance", headers=headers)
    assert done.status_code == 409


def test_medwaste_handover_and_overdue_alert(client, headers, base_data):
    fresh = client.post(
        "/api/medwaste",
        json={
            "org_id": base_data["township"]["id"],
            "waste_type": "infectious",
            "weight_kg": 3.5,
            "collected_date": "2026-08-09",
        },
        headers=headers,
    ).json()
    stale = client.post(
        "/api/medwaste",
        json={
            "org_id": base_data["township"]["id"],
            "waste_type": "sharp",
            "weight_kg": 1.2,
            "collected_date": "2026-08-01",
        },
        headers=headers,
    ).json()

    # 8月10日视角：只有8月1日收集且未交接的滞留预警
    alerts = client.get("/api/medwaste/alerts?today=2026-08-10", headers=headers).json()
    assert [w["id"] for w in alerts] == [stale["id"]]

    handed = client.post(
        f"/api/medwaste/{stale['id']}/handover",
        json={"handler_name": "转运员王强"},
        headers=headers,
    ).json()
    assert handed["status"] == "handed_over"

    # 交接后预警清零
    assert client.get("/api/medwaste/alerts?today=2026-08-10", headers=headers).json() == []
    assert fresh["status"] == "collected"


def test_performance_scorecards(client, headers, base_data):
    data = client.get("/api/performance/orgs", headers=headers).json()
    assert sum(data["weights"].values()) == 100
    cards = {c["org_name"]: c for c in data["scorecards"]}
    township = cards["北镇卫生院"]
    # 本模块测试中该院有1条家医履约记录：15 * min(1,5)/5 = 3 分
    assert township["detail"]["contract_services"] == 1
    assert township["score"] == 3.0
    # 排序为降序
    scores = [c["score"] for c in data["scorecards"]]
    assert scores == sorted(scores, reverse=True)
