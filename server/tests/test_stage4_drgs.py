"""M12 DRGs 简化版：分组目录种子化、出院自动入组、CMI 与组均费用统计。"""
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
    orgs, wards, beds = {}, {}, {}
    for key, name in [("a", "DRG县医院"), ("b", "DRG卫生院")]:
        org = client.post(
            "/api/organizations",
            json={"name": name, "org_type": "lead_hospital", "level": "county"},
            headers=admin,
        ).json()
        ward = client.post(
            "/api/inpatient/wards", json={"org_id": org["id"], "name": "病区"}, headers=admin
        ).json()
        orgs[key], wards[key] = org, ward
        beds[key] = [
            client.post(
                "/api/inpatient/beds",
                json={"ward_id": ward["id"], "bed_no": f"{key}-{i}"},
                headers=admin,
            ).json()
            for i in range(1, 4)
        ]
    client.post(
        "/api/users",
        json={"username": "drg_doc", "password": "pass123456", "role": "doctor", "org_id": org["id"]},
        headers=admin,
    )
    doctor = login(client, "drg_doc", "pass123456")
    patients = [
        client.post(
            "/api/patients",
            json={"name": f"DRG患者{i}", "id_card": f"32098119900101{9100 + i}"},
            headers=admin,
        ).json()
        for i in range(4)
    ]
    return {"orgs": orgs, "wards": wards, "beds": beds, "doctor": doctor, "patients": patients}


def _discharge_case(client, setup, admin, org_key, bed_idx, patient, diagnosis, cost):
    """入院→病案首页（自动入组）→出院，返回病案首页响应。

    第九轮：这些入院跨 a、b 两家机构，用 admin 播种。原先用单机构 doctor 写
    别家机构的入院，正是机构写守卫该拦的——测试是要跨机构聚合统计，
    不是要单个医生替别家做病案，故播种走 admin（全域）。"""
    adm = client.post(
        "/api/inpatient/admissions",
        json={
            "patient_id": patient["id"],
            "ward_id": setup["wards"][org_key]["id"],
            "bed_id": setup["beds"][org_key][bed_idx]["id"],
            "diagnosis_name": diagnosis,
        },
        headers=admin,
    ).json()
    summary = client.post(
        f"/api/inpatient/admissions/{adm['id']}/case-summary",
        json={"discharge_diagnosis": diagnosis, "total_cost": cost, "outcome": "好转"},
        headers=admin,
    ).json()
    discharged = client.post(
        f"/api/inpatient/admissions/{adm['id']}/discharge", headers=admin
    )
    assert discharged.status_code == 200, discharged.text
    return summary


def test_drg_groups_seeded(client, admin):
    groups = client.get("/api/drgs/groups", headers=admin).json()
    assert len(groups) >= 10
    codes = {g["code"] for g in groups}
    assert {"ES31", "BR23", "KS11"} <= codes


def test_case_summary_auto_grouping(client, admin, setup):
    s1 = _discharge_case(client, setup, admin, "a", 0, setup["patients"][0], "社区获得性肺炎", 6000)
    assert s1["drg_code"] == "ES31"
    assert s1["drg_weight"] == 0.95
    assert s1["drg"]["drg_name"] == "呼吸系统感染（肺炎）"

    # 块3：任何分组均未命中 → 落入 QY 兜底组（不再是"不入组"）
    s2 = _discharge_case(client, setup, admin, "a", 1, setup["patients"][1], "罕见代谢病", 3000)
    assert s2["drg_code"] == "QY"
    assert s2["drg"]["fallback"] is True

    # GET 病案首页回读含 DRG 字段
    admissions = client.get(
        f"/api/inpatient/admissions?patient_id={setup['patients'][0]['id']}", headers=admin
    ).json()
    read_back = client.get(
        f"/api/inpatient/admissions/{admissions[0]['id']}/case-summary", headers=admin
    ).json()
    assert read_back["drg_code"] == "ES31"


def test_drg_stats_cmi_and_group_costs(client, admin, setup):
    # 机构B：脑梗（权重1.35）+ 糖尿病（0.78）
    _discharge_case(client, setup, admin, "b", 0, setup["patients"][2], "急性脑梗死", 12000)
    _discharge_case(client, setup, admin, "b", 1, setup["patients"][3], "2型糖尿病", 4000)

    stats = client.get("/api/drgs/stats", headers=admin).json()
    by_org = {r["org_name"]: r for r in stats["orgs"]}
    a, b = by_org["DRG县医院"], by_org["DRG卫生院"]
    # 机构A：2例（1例入组 ES31）→ CMI=0.95/1，入组率50%
    assert a["cases"] == 2
    assert a["grouped"] == 1
    assert a["grouped_pct"] == 50.0
    assert a["cmi"] == 0.95
    # 机构B：2例全入组 → CMI=(1.35+0.78)/2
    assert b["grouped"] == 2
    assert b["cmi"] == round((1.35 + 0.78) / 2, 3)

    by_group = {g["drg_code"]: g for g in stats["groups"]}
    assert by_group["ES31"]["cases"] == 1
    assert by_group["ES31"]["avg_cost"] == 6000
    assert by_group["BR23"]["avg_cost"] == 12000


def test_drg_group_admin_maintenance(client, admin, setup):
    denied = client.post(
        "/api/drgs/groups",
        json={"code": "XX99", "name": "越权组", "base_weight": 1.0},
        headers=setup["doctor"],
    )
    assert denied.status_code == 403
    created = client.post(
        "/api/drgs/groups",
        json={"code": "XX99", "name": "测试组", "base_weight": 1.2, "keywords": "测试病"},
        headers=admin,
    )
    assert created.status_code == 201
    dup = client.post(
        "/api/drgs/groups",
        json={"code": "XX99", "name": "重复", "base_weight": 1},
        headers=admin,
    )
    assert dup.status_code == 409
    patched = client.patch(
        f"/api/drgs/groups/{created.json()['id']}", json={"base_weight": 1.5}, headers=admin
    )
    assert patched.json()["base_weight"] == 1.5
