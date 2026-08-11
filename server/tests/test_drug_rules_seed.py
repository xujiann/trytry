"""块2：审方规则库扩充——50 条常用药品规则的典型拦截场景与点评规则化。"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.data.drug_rules_seed import SEED_DRUG_RULES
from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def setup(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "审方规则测试医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    adult = client.post(
        "/api/patients",
        json={"name": "审方成人", "id_card": "320981197001011234", "gender": "男", "birth_date": "1970-01-01"},
        headers=admin,
    ).json()
    child = client.post(
        "/api/patients",
        json={"name": "审方儿童", "id_card": "320981201801011235", "gender": "男", "birth_date": "2018-01-01"},
        headers=admin,
    ).json()
    return {"org": org, "adult": adult, "child": child}


def _rx(client, headers, patient_id, org_id, items, diagnosis=""):
    return client.post(
        "/api/prescriptions",
        json={"patient_id": patient_id, "org_id": org_id, "diagnosis_name": diagnosis, "items": items},
        headers=headers,
    ).json()


def test_seed_rules_loaded_and_idempotent(client, admin):
    rules = {r["drug_code"]: r for r in client.get("/api/prescriptions/rules", headers=admin).json()}
    assert len(SEED_DRUG_RULES) == 50
    assert len(set(r["drug_code"] for r in SEED_DRUG_RULES)) == 50
    for seed in SEED_DRUG_RULES:
        assert seed["drug_code"] in rules
    # 每条规则至少给出日剂量上限与点评要点
    assert all(r["max_daily_dose"] > 0 and r["review_points"] for r in SEED_DRUG_RULES)
    # 肝肾功能提示字段已入库
    assert "eGFR" in rules["A10BA02"]["renal_hepatic_note"]

    before = len(rules)
    with TestClient(app):
        pass
    assert len(client.get("/api/prescriptions/rules", headers=admin).json()) == before


def test_daily_dose_limit_intercepts(client, admin, setup):
    """日剂量上限：二甲双胍 2550mg/日为限，3000mg 触发系统审拦截。"""
    ok = _rx(client, admin, setup["adult"]["id"], setup["org"]["id"],
             [{"drug_code": "A10BA02", "drug_name": "二甲双胍", "daily_dose": 1500}], "2型糖尿病")
    assert ok["status"] == "auto_passed"

    over = _rx(client, admin, setup["adult"]["id"], setup["org"]["id"],
               [{"drug_code": "A10BA02", "drug_name": "二甲双胍", "daily_dose": 3000}], "2型糖尿病")
    assert over["status"] == "pending_review"
    assert "超过上限" in over["review_comment"]


def test_warfarin_aspirin_interaction_intercepts(client, admin, setup):
    """相互作用药对：华法林 + 阿司匹林同方 → 转药师审。"""
    rx = _rx(client, admin, setup["adult"]["id"], setup["org"]["id"],
             [
                 {"drug_code": "B01AA03", "drug_name": "华法林", "daily_dose": 3},
                 {"drug_code": "B01AC06", "drug_name": "阿司匹林", "daily_dose": 100},
             ], "心房颤动")
    assert rx["status"] == "pending_review"
    assert "药物相互作用" in rx["review_comment"]
    assert "华法林" in rx["review_comment"] and "阿司匹林" in rx["review_comment"]


def test_nsaid_peptic_ulcer_contraindication_intercepts(client, admin, setup):
    """禁忌诊断：NSAIDs（布洛芬）+ 消化性溃疡 → 转药师审。"""
    ok = _rx(client, admin, setup["adult"]["id"], setup["org"]["id"],
             [{"drug_code": "M01AE01", "drug_name": "布洛芬", "daily_dose": 600}], "急性上呼吸道感染")
    assert ok["status"] == "auto_passed"

    blocked = _rx(client, admin, setup["adult"]["id"], setup["org"]["id"],
                  [{"drug_code": "M01AE01", "drug_name": "布洛芬", "daily_dose": 600}], "消化性溃疡")
    assert blocked["status"] == "pending_review"
    assert "禁忌诊断" in blocked["review_comment"]


def test_quinolone_child_special_group_intercepts(client, admin, setup):
    """特殊人群：喹诺酮类（左氧氟沙星）用于儿童 → 转药师审。"""
    adult_rx = _rx(client, admin, setup["adult"]["id"], setup["org"]["id"],
                   [{"drug_code": "J01MA12", "drug_name": "左氧氟沙星", "daily_dose": 500}], "社区获得性肺炎")
    assert adult_rx["status"] == "auto_passed"

    child_rx = _rx(client, admin, setup["child"]["id"], setup["org"]["id"],
                   [{"drug_code": "J01MA12", "drug_name": "左氧氟沙星", "daily_dose": 250}], "社区获得性肺炎")
    assert child_rx["status"] == "pending_review"
    assert "儿童" in child_rx["review_comment"]


def test_renal_hepatic_note_is_advisory_not_blocking(client, admin, setup):
    """肝肾功能提示为非拦截提醒：随处方返回但不改变审方状态。"""
    rx = _rx(client, admin, setup["adult"]["id"], setup["org"]["id"],
             [{"drug_code": "A10BA02", "drug_name": "二甲双胍", "daily_dose": 1000}], "2型糖尿病")
    assert rx["status"] == "auto_passed"
    assert any("肝肾功能提示" in a and "eGFR" in a for a in rx["advisories"])


def test_review_points_endpoint(client, admin, setup):
    """处方点评规则化：按方汇总点评要点、肝肾提示与规则覆盖率。"""
    rx = _rx(client, admin, setup["adult"]["id"], setup["org"]["id"],
             [
                 {"drug_code": "B01AA03", "drug_name": "华法林", "daily_dose": 12},
                 {"drug_code": "NORULE9", "drug_name": "自制药", "daily_dose": 1},
             ], "心房颤动")
    points = client.get(f"/api/prescriptions/{rx['id']}/review-points", headers=admin).json()
    warfarin = next(i for i in points["items"] if i["drug_code"] == "B01AA03")
    assert warfarin["dose_exceeded"] is True
    assert "INR" in warfarin["review_points"]
    assert warfarin["renal_hepatic_note"]
    unknown = next(i for i in points["items"] if i["drug_code"] == "NORULE9")
    assert unknown["no_rule"] is True and unknown["review_points"] == ""
    assert points["rule_coverage_pct"] == 50.0
    assert client.get("/api/prescriptions/999999/review-points", headers=admin).status_code == 404


def test_local_rule_override_is_preserved_on_restart(client, admin):
    """本地调整过的规则不被种子覆盖（幂等只补不改）。"""
    client.post(
        "/api/prescriptions/rules/import",
        json=[{"drug_code": "A10BA02", "max_daily_dose": 1700, "dose_unit": "mg", "note": "本院调低"}],
        headers=admin,
    )
    with TestClient(app):
        pass
    rules = {r["drug_code"]: r for r in client.get("/api/prescriptions/rules", headers=admin).json()}
    assert rules["A10BA02"]["max_daily_dose"] == 1700
