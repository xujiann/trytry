"""M8 费用结算：收费目录、费用明细累计、门诊/住院结算与出院联动。"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def setup(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "结算测试医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "结算患者", "id_card": "320981199001017001"},
        headers=admin,
    ).json()
    users = {}
    for username, role in [("bill_doc", "doctor"), ("bill_op", "operator")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
        users[role] = login(client, username, "pass123456")
    ward = client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "综合病区"}, headers=admin
    ).json()
    bed = client.post(
        "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "B-01"}, headers=admin
    ).json()
    # 收费目录
    for code, name, cat, price in [
        ("BED-A", "床位费(普通)", "bed", 60),
        ("LAB-K", "血钾测定", "exam", 25.5),
        ("REG-01", "门诊诊查费", "treatment", 15),
    ]:
        r = client.post(
            "/api/billing/charge-items",
            json={"code": code, "name": name, "category": cat, "price": price},
            headers=admin,
        )
        assert r.status_code == 201, r.text
    return {"org": org, "patient": patient, "ward": ward, "bed": bed, **users}


def test_charge_item_admin_only_and_dict_link(client, admin, setup):
    denied = client.post(
        "/api/billing/charge-items",
        json={"code": "X1", "name": "越权项目", "price": 1},
        headers=setup["operator"],
    )
    assert denied.status_code == 403
    dup = client.post(
        "/api/billing/charge-items",
        json={"code": "BED-A", "name": "重复", "price": 1},
        headers=admin,
    )
    assert dup.status_code == 409
    # 收费字典配置条目后：目录外编码 422，目录内编码放行
    client.post(
        "/api/dictionaries/charge/entries",
        json={"code": "CHG-001", "name": "字典内收费项"},
        headers=admin,
    )
    blocked = client.post(
        "/api/billing/charge-items",
        json={"code": "NOT-IN-DICT", "name": "字典外", "price": 5},
        headers=admin,
    )
    assert blocked.status_code == 422
    allowed = client.post(
        "/api/billing/charge-items",
        json={"code": "CHG-001", "name": "字典内收费项", "price": 8},
        headers=admin,
    )
    assert allowed.status_code == 201
    # 调价
    patched = client.patch(
        f"/api/billing/charge-items/{allowed.json()['id']}", json={"price": 9.5}, headers=admin
    )
    assert patched.json()["price"] == 9.5


def test_inpatient_billing_blocks_discharge_until_settled(client, admin, setup):
    adm = client.post(
        "/api/inpatient/admissions",
        json={
            "patient_id": setup["patient"]["id"],
            "ward_id": setup["ward"]["id"],
            "bed_id": setup["bed"]["id"],
            "diagnosis_name": "肺炎",
        },
        headers=setup["operator"],
    ).json()
    setup["admission"] = adm
    # 住院计费：床位费3天 + 血钾2次
    for code, qty in [("BED-A", 3), ("LAB-K", 2)]:
        r = client.post(
            "/api/billing/details",
            json={
                "patient_id": setup["patient"]["id"],
                "admission_id": adm["id"],
                "item_code": code,
                "quantity": qty,
            },
            headers=setup["operator"],
        )
        assert r.status_code == 201, r.text
    details = client.get(f"/api/billing/details?admission_id={adm['id']}", headers=admin).json()
    assert len(details) == 2
    total = round(sum(d["amount"] for d in details), 2)
    assert total == round(60 * 3 + 25.5 * 2, 2)

    # 病案首页填写后，费用未结清仍不可出院（M7-M8 联动）
    client.post(
        f"/api/inpatient/admissions/{adm['id']}/case-summary",
        json={"discharge_diagnosis": "肺炎治愈", "total_cost": total, "drug_cost": 0, "outcome": "治愈"},
        headers=setup["doctor"],
    )
    blocked = client.post(
        f"/api/inpatient/admissions/{adm['id']}/discharge", headers=setup["doctor"]
    )
    assert blocked.status_code == 409
    assert "未结清" in blocked.json()["detail"]

    # 医保支付超总额 → 422
    over = client.post(
        "/api/billing/settlements",
        json={"bill_type": "inpatient", "admission_id": adm["id"], "insurance_pay": total + 1},
        headers=setup["operator"],
    )
    assert over.status_code == 422

    settled = client.post(
        "/api/billing/settlements",
        json={"bill_type": "inpatient", "admission_id": adm["id"], "insurance_pay": 150},
        headers=setup["operator"],
    )
    assert settled.status_code == 201, settled.text
    body = settled.json()
    assert body["total_amount"] == total
    assert body["self_pay"] == round(total - 150, 2)
    # 医保分担联动生成 InsuranceSettlement（基金监测口径）
    assert body["insurance_settlement_id"] is not None
    fund = client.get("/api/insurance/fund-stats", headers=admin).json()
    assert fund["insurance_pay_total"] >= 150

    # 明细全部回填结算单号
    details = client.get(
        f"/api/billing/details?admission_id={adm['id']}&settled=true", headers=admin
    ).json()
    assert len(details) == 2

    # 重复结算：无未结清明细 → 422
    again = client.post(
        "/api/billing/settlements",
        json={"bill_type": "inpatient", "admission_id": adm["id"], "insurance_pay": 0},
        headers=setup["operator"],
    )
    assert again.status_code == 422

    # 结清后可出院
    discharged = client.post(
        f"/api/inpatient/admissions/{adm['id']}/discharge", headers=setup["doctor"]
    )
    assert discharged.status_code == 200
    # 出院后不可继续计费
    late = client.post(
        "/api/billing/details",
        json={
            "patient_id": setup["patient"]["id"],
            "admission_id": adm["id"],
            "item_code": "BED-A",
        },
        headers=setup["operator"],
    )
    assert late.status_code == 409


def test_outpatient_billing_by_encounter(client, admin, setup):
    encounter = client.post(
        "/api/encounters",
        json={
            "patient_id": setup["patient"]["id"],
            "org_id": setup["org"]["id"],
            "diagnosis_name": "上呼吸道感染",
            "encounter_type": "outpatient",
        },
        headers=setup["doctor"],
    ).json()
    r = client.post(
        "/api/billing/details",
        json={
            "patient_id": setup["patient"]["id"],
            "encounter_id": encounter["id"],
            "item_code": "REG-01",
        },
        headers=setup["doctor"],
    )
    assert r.status_code == 201
    # admission/encounter 二选一校验
    both = client.post(
        "/api/billing/details",
        json={
            "patient_id": setup["patient"]["id"],
            "admission_id": setup["admission"]["id"],
            "encounter_id": encounter["id"],
            "item_code": "REG-01",
        },
        headers=setup["doctor"],
    )
    assert both.status_code == 422

    settled = client.post(
        "/api/billing/settlements",
        json={"bill_type": "outpatient", "encounter_id": encounter["id"], "insurance_pay": 10},
        headers=setup["operator"],
    )
    assert settled.status_code == 201
    assert settled.json()["total_amount"] == 15
    assert settled.json()["self_pay"] == 5

    stats = client.get("/api/billing/stats", headers=admin).json()
    by_type = {r["bill_type"]: r for r in stats}
    assert by_type["outpatient"]["count"] == 1
    assert by_type["outpatient"]["avg_amount"] == 15
    assert by_type["inpatient"]["count"] == 1
