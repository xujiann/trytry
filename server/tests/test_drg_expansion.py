"""块3：DRG 分组扩充——62 组 + QY 兜底组、多关键词与主手术匹配、MDC 汇总。"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.data.drg_groups_seed import FALLBACK_DRG_GROUP, SEED_DRG_GROUPS
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
    org = client.post(
        "/api/organizations",
        json={"name": "DRG扩充县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    ward = client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "综合病区"}, headers=admin
    ).json()
    client.post(
        "/api/users",
        json={"username": "drgx_doc", "password": "pass123456", "role": "doctor", "org_id": org["id"]},
        headers=admin,
    )
    return {"org": org, "ward": ward, "doctor": login(client, "drgx_doc", "pass123456"), "seq": [0]}


def _case(client, setup, admin, diagnosis, operation="", cost=5000):
    """建床→入院→病案首页（自动入组）→出院，返回病案首页响应。"""
    n = setup["seq"][0]
    setup["seq"][0] += 1
    bed = client.post(
        "/api/inpatient/beds",
        json={"ward_id": setup["ward"]["id"], "bed_no": f"x-{n}"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": f"DRGX患者{n}", "id_card": f"32098119900101{7000 + n}"},
        headers=admin,
    ).json()
    adm = client.post(
        "/api/inpatient/admissions",
        json={
            "patient_id": patient["id"],
            "ward_id": setup["ward"]["id"],
            "bed_id": bed["id"],
            "diagnosis_name": diagnosis,
        },
        headers=setup["doctor"],
    ).json()
    summary = client.post(
        f"/api/inpatient/admissions/{adm['id']}/case-summary",
        json={
            "discharge_diagnosis": diagnosis,
            "operation": operation,
            "total_cost": cost,
            "outcome": "好转",
        },
        headers=setup["doctor"],
    ).json()
    client.post(f"/api/inpatient/admissions/{adm['id']}/discharge", headers=setup["doctor"])
    return summary


def test_seed_expanded_to_60_plus_groups(client, admin):
    assert len(SEED_DRG_GROUPS) >= 60
    assert len({g["code"] for g in SEED_DRG_GROUPS}) == len(SEED_DRG_GROUPS)
    groups = client.get("/api/drgs/groups", headers=admin).json()
    codes = {g["code"] for g in groups}
    assert len(groups) >= 60
    # 原 10 组编码与权重保持不变（兼容存量入组数据）
    assert {"ES31", "BR23", "FM19", "HZ23", "GD29", "KS11", "FV29", "LU14", "OB25", "GB29"} <= codes
    assert next(g for g in groups if g["code"] == "ES31")["base_weight"] == 0.95
    # 覆盖主要 MDC：神经/呼吸/循环/消化/骨骼肌肉/泌尿/妇产/儿科等
    mdcs = {g["mdc"] for g in groups}
    assert {"MDCB", "MDCE", "MDCF", "MDCG", "MDCI", "MDCL", "MDCO", "MDCP"} <= mdcs
    # QY 兜底组已入库并带标志
    fallback = next(g for g in groups if g["code"] == FALLBACK_DRG_GROUP["code"])
    assert fallback["is_fallback"] is True
    # 按 MDC 过滤
    resp = client.get("/api/drgs/groups?mdc=MDCE", headers=admin).json()
    assert resp and all(g["mdc"] == "MDCE" for g in resp)


def test_multi_keyword_matching_prefers_more_specific_group(client, admin, setup):
    """多关键词：命中更多/更长关键词的组优先。"""
    # "慢性阻塞性肺疾病急性加重" 同时含"慢性阻塞性肺"（ES15）与其他弱匹配，取 ES15
    copd = _case(client, setup, admin, "慢性阻塞性肺疾病急性加重伴感染")
    assert copd["drg_code"] == "ES15"
    # 单纯肺炎仍入原 ES31 组，权重不变
    pneumonia = _case(client, setup, admin, "社区获得性肺炎")
    assert pneumonia["drg_code"] == "ES31"
    assert pneumonia["drg_weight"] == 0.95


def test_procedure_flag_separates_surgical_from_medical(client, admin, setup):
    """主手术标志：同一诊断有无手术分入不同组，未做手术不得入外科组。"""
    conservative = _case(client, setup, admin, "急性结石性胆囊炎", operation="", cost=6000)
    assert conservative["drg_code"] == "HZ23"  # 内科保守 → 胆囊疾患组

    surgical = _case(
        client, setup, admin, "急性结石性胆囊炎", operation="腹腔镜胆囊切除术", cost=18000
    )
    assert surgical["drg_code"] == "HC19"  # 命中主手术 → 胆囊切除术组
    assert surgical["drg_weight"] == 1.55

    # 骨折未手术 → 非手术组；行内固定 → 手术组
    conserve_fx = _case(client, setup, admin, "左桡骨远端骨折", operation="石膏外固定")
    assert conserve_fx["drg_code"] == "IU13"
    operate_fx = _case(
        client, setup, admin, "左股骨干骨折", operation="切开复位钢板内固定术", cost=25000
    )
    assert operate_fx["drg_code"] == "IF29"


def test_unmatched_case_falls_into_qy_group(client, admin, setup):
    unmatched = _case(client, setup, admin, "某罕见综合征", cost=3000)
    assert unmatched["drg_code"] == "QY"
    assert unmatched["drg"]["fallback"] is True
    assert unmatched["drg_weight"] == FALLBACK_DRG_GROUP["base_weight"]


def test_stats_lists_fallback_separately_and_aggregates_by_mdc(client, admin, setup):
    stats = client.get("/api/drgs/stats", headers=admin).json()
    org = next(r for r in stats["orgs"] if r["org_name"] == "DRG扩充县医院")
    # QY 兜底例数单列，且不计入正式入组与 CMI 分母
    assert org["fallback"] >= 1
    assert org["grouped"] == org["cases"] - org["fallback"]
    assert org["fallback_pct"] == round(org["fallback"] * 100.0 / org["cases"], 2)
    assert org["cmi"] > 0

    by_code = {g["drg_code"]: g for g in stats["groups"]}
    assert by_code["QY"]["fallback"] is True
    assert by_code["HC19"]["fallback"] is False
    assert by_code["HC19"]["mdc"] == "MDCH"

    mdcs = {m["mdc"]: m for m in stats["mdcs"]}
    assert "MDCE" in mdcs and "MDCH" in mdcs
    assert mdcs["MDCH"]["cases"] == 2  # HZ23 + HC19 各 1 例
    assert mdcs["MDCH"]["mdc_name"].startswith("肝胆胰")
    assert mdcs["QY"]["fallback"] is True
    assert sum(m["cases"] for m in stats["mdcs"]) == org["cases"]


def test_admin_can_add_group_with_procedure_rule(client, admin, setup):
    created = client.post(
        "/api/drgs/groups",
        json={
            "code": "ZT01",
            "name": "自定义腔镜组",
            "base_weight": 2.0,
            "keywords": "自定义病",
            "mdc": "MDCG",
            "mdc_name": "消化系统疾病及功能障碍",
            "procedure_keywords": "自定义腔镜术",
            "require_procedure": True,
        },
        headers=admin,
    )
    assert created.status_code == 201
    # 未做手术 → 不入该组，落 QY
    assert _case(client, setup, admin, "自定义病")["drg_code"] == "QY"
    # 做了手术 → 入组
    assert _case(client, setup, admin, "自定义病", operation="自定义腔镜术")["drg_code"] == "ZT01"
