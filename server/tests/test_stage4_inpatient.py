"""M7 住院与床位：病区/床位资源、入出转状态机、住院医嘱、病案首页。"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def setup(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "住院测试医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    org2 = client.post(
        "/api/organizations",
        json={"name": "住院测试卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    p1 = client.post(
        "/api/patients",
        json={"name": "住院患者一", "id_card": "320981199001016001"},
        headers=admin,
    ).json()
    p2 = client.post(
        "/api/patients",
        json={"name": "住院患者二", "id_card": "320981199001016002"},
        headers=admin,
    ).json()
    users = {}
    for username, role in [("inp_doc", "doctor"), ("inp_op", "operator")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
        users[role] = login(client, username, "pass123456")
    ward1 = client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "内科病区"}, headers=admin
    ).json()
    ward2 = client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "外科病区"}, headers=admin
    ).json()
    beds = {}
    for ward, no in [(ward1, "1-01"), (ward1, "1-02"), (ward2, "2-01")]:
        b = client.post(
            "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": no}, headers=admin
        ).json()
        beds[no] = b
    return {
        "org": org, "org2": org2, "p1": p1, "p2": p2,
        "ward1": ward1, "ward2": ward2, "beds": beds, **users,
    }


def test_ward_bed_admin_only(client, setup):
    denied = client.post(
        "/api/inpatient/wards",
        json={"org_id": setup["org"]["id"], "name": "越权病区"},
        headers=setup["operator"],
    )
    assert denied.status_code == 403
    dup = client.post(
        "/api/inpatient/beds",
        json={"ward_id": setup["ward1"]["id"], "bed_no": "1-01"},
        headers=login(client, "admin", "admin123"),
    )
    assert dup.status_code == 409


def test_admission_occupies_bed_atomically(client, admin, setup):
    bed = setup["beds"]["1-01"]
    adm = client.post(
        "/api/inpatient/admissions",
        json={
            "patient_id": setup["p1"]["id"],
            "ward_id": setup["ward1"]["id"],
            "bed_id": bed["id"],
            "doctor_name": "王医师",
            "diagnosis_name": "肺炎",
        },
        headers=setup["operator"],
    )
    assert adm.status_code == 201, adm.text
    setup["admission"] = adm.json()
    assert setup["admission"]["status"] == "admitted"
    assert setup["admission"]["org_id"] == setup["org"]["id"]

    # 床位已占用：他人抢占同床 → 409
    conflict = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": setup["p2"]["id"], "ward_id": setup["ward1"]["id"], "bed_id": bed["id"]},
        headers=setup["operator"],
    )
    assert conflict.status_code == 409
    beds = client.get(f"/api/inpatient/beds?ward_id={setup['ward1']['id']}", headers=admin).json()
    assert next(b["status"] for b in beds if b["id"] == bed["id"]) == "occupied"

    # 同一患者在院不可重复入院
    dup = client.post(
        "/api/inpatient/admissions",
        json={
            "patient_id": setup["p1"]["id"],
            "ward_id": setup["ward1"]["id"],
            "bed_id": setup["beds"]["1-02"]["id"],
        },
        headers=setup["operator"],
    )
    assert dup.status_code == 409

    # 入院同步生成 inpatient 类型就诊记录（360 数据源）
    encounters = client.get(
        f"/api/encounters?patient_id={setup['p1']['id']}", headers=admin
    ).json()
    assert any(e["encounter_type"] == "inpatient" for e in encounters)


def test_transfer_moves_bed_occupancy(client, admin, setup):
    aid = setup["admission"]["id"]
    old_bed = setup["beds"]["1-01"]
    new_bed = setup["beds"]["2-01"]
    # 经办无权转科
    assert client.post(
        f"/api/inpatient/admissions/{aid}/transfer",
        json={"ward_id": setup["ward2"]["id"], "bed_id": new_bed["id"]},
        headers=setup["operator"],
    ).status_code == 403
    moved = client.post(
        f"/api/inpatient/admissions/{aid}/transfer",
        json={"ward_id": setup["ward2"]["id"], "bed_id": new_bed["id"]},
        headers=setup["doctor"],
    )
    assert moved.status_code == 200
    assert moved.json()["ward_id"] == setup["ward2"]["id"]
    all_beds = client.get("/api/inpatient/beds", headers=admin).json()
    statuses = {b["id"]: b["status"] for b in all_beds}
    assert statuses[old_bed["id"]] == "free"
    assert statuses[new_bed["id"]] == "occupied"


def test_inpatient_orders_doctor_only(client, setup):
    aid = setup["admission"]["id"]
    denied = client.post(
        "/api/inpatient/orders",
        json={"admission_id": aid, "order_type": "long", "content": "一级护理"},
        headers=setup["operator"],
    )
    assert denied.status_code == 403
    order = client.post(
        "/api/inpatient/orders",
        json={"admission_id": aid, "order_type": "long", "content": "一级护理"},
        headers=setup["doctor"],
    )
    assert order.status_code == 201
    oid = order.json()["id"]
    stopped = client.post(f"/api/inpatient/orders/{oid}/stop", headers=setup["doctor"])
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "stopped"
    assert stopped.json()["stopped_by_name"] != ""
    again = client.post(f"/api/inpatient/orders/{oid}/stop", headers=setup["doctor"])
    assert again.status_code == 409


def test_discharge_requires_case_summary(client, admin, setup):
    aid = setup["admission"]["id"]
    blocked = client.post(f"/api/inpatient/admissions/{aid}/discharge", headers=setup["doctor"])
    assert blocked.status_code == 409
    assert "病案首页" in blocked.json()["detail"]

    bad = client.post(
        f"/api/inpatient/admissions/{aid}/case-summary",
        json={"discharge_diagnosis": "肺炎痊愈", "total_cost": 100, "drug_cost": 500},
        headers=setup["doctor"],
    )
    assert bad.status_code == 422  # 药费超总费用

    summary = client.post(
        f"/api/inpatient/admissions/{aid}/case-summary",
        json={
            "discharge_diagnosis": "社区获得性肺炎",
            "operation": "",
            "total_cost": 5200.5,
            "drug_cost": 1800,
            "outcome": "治愈",
        },
        headers=setup["doctor"],
    )
    assert summary.status_code == 201, summary.text
    dup = client.post(
        f"/api/inpatient/admissions/{aid}/case-summary",
        json={"discharge_diagnosis": "重复"},
        headers=setup["doctor"],
    )
    assert dup.status_code == 409

    # 开一条执行中医嘱，验证出院自动停止
    client.post(
        "/api/inpatient/orders",
        json={"admission_id": aid, "order_type": "temp", "content": "出院带药"},
        headers=setup["doctor"],
    ).json()

    discharged = client.post(
        f"/api/inpatient/admissions/{aid}/discharge", headers=setup["doctor"]
    )
    assert discharged.status_code == 200
    body = discharged.json()
    assert body["status"] == "discharged"
    assert body["discharged_at"] is not None

    # 床位释放
    all_beds = client.get("/api/inpatient/beds", headers=admin).json()
    assert all(b["status"] == "free" for b in all_beds)
    # 医嘱全部停止
    orders = client.get(f"/api/inpatient/orders?admission_id={aid}", headers=admin).json()
    assert all(o["status"] == "stopped" for o in orders)
    # 出院后不可再开医嘱/再出院
    assert client.post(
        "/api/inpatient/orders",
        json={"admission_id": aid, "order_type": "temp", "content": "补开"},
        headers=setup["doctor"],
    ).status_code == 409
    assert client.post(
        f"/api/inpatient/admissions/{aid}/discharge", headers=setup["doctor"]
    ).status_code == 409


def test_inpatient_stats_occupancy(client, admin, setup):
    # 再入院一名患者占床后看统计
    adm = client.post(
        "/api/inpatient/admissions",
        json={
            "patient_id": setup["p2"]["id"],
            "ward_id": setup["ward1"]["id"],
            "bed_id": setup["beds"]["1-02"]["id"],
            "diagnosis_name": "阑尾炎",
        },
        headers=setup["operator"],
    )
    assert adm.status_code == 201
    stats = client.get("/api/inpatient/stats", headers=admin).json()
    row = next(r for r in stats if r["org_id"] == setup["org"]["id"])
    assert row["beds_total"] == 3
    assert row["beds_occupied"] == 1
    assert row["in_hospital"] == 1
    assert row["discharged_total"] == 1
    assert row["occupancy_pct"] == round(1 * 100.0 / 3, 2)
