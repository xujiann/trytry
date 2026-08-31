"""块1：慢病病种目录扩展（8 病种）、目录驱动分级、通用指标、随访周期自动建议。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

from conftest import login


@pytest.fixture(scope="module")
def h(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def base(client, h):
    org = client.post(
        "/api/organizations",
        json={"name": "慢病目录测试卫生院", "org_type": "township", "level": "township"},
        headers=h,
    ).json()
    patients = [
        client.post(
            "/api/patients",
            json={"name": f"慢病患者{i}", "id_card": f"32098119900101{i:04d}", "gender": "男"},
            headers=h,
        ).json()
        for i in range(8)
    ]
    return {"org": org, "patients": patients}


def _register(client, h, base, index, disease, **extra):
    return client.post(
        "/api/chronic",
        json={
            "patient_id": base["patients"][index]["id"],
            "disease": disease,
            "managed_by_org_id": base["org"]["id"],
            **extra,
        },
        headers=h,
    )


def test_disease_type_catalog_seeded(client, h):
    types = client.get("/api/chronic/disease-types", headers=h).json()
    codes = {t["code"]: t for t in types}
    assert {
        "hypertension", "diabetes", "copd", "chd",
        "stroke", "severe_mental", "cancer", "tuberculosis",
    } <= set(codes)
    assert codes["hypertension"]["name"] == "高血压"
    assert codes["tuberculosis"]["followup_interval_days"] == 30
    # 分级规则以指标名 + 阈值区间描述，血压阈值与既有硬编码一致
    sbp = codes["hypertension"]["level_rules"]["metrics"][0]
    assert (sbp["key"], sbp["level3"], sbp["level2"]) == ("sbp", 160, 140)
    assert "限盐" in codes["hypertension"]["guidance"]


def test_disease_type_catalog_is_idempotent_on_restart(client, h):
    """重复启动不重复入库（幂等种子化）。"""
    before = len(client.get("/api/chronic/disease-types", headers=h).json())
    with TestClient(app):
        pass
    assert len(client.get("/api/chronic/disease-types", headers=h).json()) == before


def test_register_validates_disease_against_catalog(client, h, base):
    # 目录内编码放行（既有三种编码不变，兼容存量）
    assert _register(client, h, base, 0, "hypertension").status_code == 201
    # 目录外编码拒绝
    bad = _register(client, h, base, 1, "not_a_disease")
    assert bad.status_code == 422
    assert "病种" in bad.json()["detail"]


def test_new_disease_uses_catalog_rules_and_generic_metrics(client, h, base):
    """严重精神障碍：用药依从性评分（越低越危）走 FollowUp.metrics 通用字段。"""
    chronic = _register(client, h, base, 2, "severe_mental").json()
    cid = chronic["id"]

    bad = client.post(
        f"/api/chronic/{cid}/followups",
        json={"metrics": {"adherence_score": 2}},
        headers=h,
    ).json()
    assert bad["level"] == 3
    assert bad["refer_up_suggested"] is True
    assert "规律服药" in bad["guidance_points"]
    assert bad["followup"]["metrics"] == {"adherence_score": 2}

    mid = client.post(
        f"/api/chronic/{cid}/followups", json={"metrics": {"adherence_score": 5}}, headers=h
    ).json()
    assert mid["level"] == 2

    good = client.post(
        f"/api/chronic/{cid}/followups", json={"metrics": {"adherence_score": 9}}, headers=h
    ).json()
    assert good["level"] == 1

    # 指标缺项时维持原级
    keep = client.post(f"/api/chronic/{cid}/followups", json={"guidance": "无指标"}, headers=h).json()
    assert keep["level"] == 1


def test_copd_and_cancer_rules(client, h, base):
    copd = _register(client, h, base, 3, "copd").json()
    high = client.post(
        f"/api/chronic/{copd['id']}/followups", json={"metrics": {"cat_score": 24}}, headers=h
    ).json()
    assert high["level"] == 3

    cancer = _register(client, h, base, 4, "cancer").json()
    mid = client.post(
        f"/api/chronic/{cancer['id']}/followups", json={"metrics": {"ecog_score": 2}}, headers=h
    ).json()
    assert mid["level"] == 2


def test_next_due_auto_suggested_by_disease_interval(client, h, base):
    """结核病周期 30 天：建档与随访未填到期日时自动建议。"""
    from datetime import date, timedelta

    chronic = _register(client, h, base, 5, "tuberculosis").json()
    expected = (date.today() + timedelta(days=30)).isoformat()
    assert chronic["next_due"] == expected

    auto = client.post(
        f"/api/chronic/{chronic['id']}/followups", json={"metrics": {"missed_doses": 0}}, headers=h
    ).json()
    assert auto["next_due"] == expected
    assert auto["next_due_suggested"] is True

    # 显式指定则以录入值为准
    manual = client.post(
        f"/api/chronic/{chronic['id']}/followups",
        json={"metrics": {"missed_doses": 0}, "next_due": "2099-01-01"},
        headers=h,
    ).json()
    assert manual["next_due"] == "2099-01-01"
    assert manual["next_due_suggested"] is False


def test_risk_metric_reads_catalog_for_new_disease(client, h, base):
    stroke = _register(client, h, base, 6, "stroke").json()
    for score in (1, 3, 5):
        client.post(
            f"/api/chronic/{stroke['id']}/followups", json={"metrics": {"mrs_score": score}}, headers=h
        )
    risk = client.get(f"/api/chronic/{stroke['id']}/risk", headers=h).json()
    assert risk["metric"] == "mrs_score"
    assert risk["recent_values"] == [1, 3, 5]
    assert risk["trend"] == "rising"
    assert risk["risk_level"] == "high"


def test_disease_type_admin_maintenance(client, h, base):
    created = client.post(
        "/api/chronic/disease-types",
        json={
            "code": "ckd",
            "name": "慢性肾脏病",
            "level_rules": {
                "metrics": [{"key": "egfr", "name": "eGFR", "direction": "low", "level3": 30, "level2": 60}]
            },
            "guidance": "优质低蛋白饮食、限盐、控制血压血糖、避免肾毒性药物",
            "followup_interval_days": 60,
        },
        headers=h,
    )
    assert created.status_code == 201
    type_id = created.json()["id"]
    assert client.post("/api/chronic/disease-types", json={"code": "ckd", "name": "重复"}, headers=h).status_code == 409

    # 新建病种立即可用于建档与分级
    chronic = _register(client, h, base, 7, "ckd").json()
    result = client.post(
        f"/api/chronic/{chronic['id']}/followups", json={"metrics": {"egfr": 25}}, headers=h
    ).json()
    assert result["level"] == 3

    # 停用后不可再建档
    assert client.patch(f"/api/chronic/disease-types/{type_id}", json={"active": False}, headers=h).status_code == 200
    assert _register(client, h, base, 1, "ckd").status_code == 422
    assert len(client.get("/api/chronic/disease-types?active=true", headers=h).json()) == 8
    assert client.patch("/api/chronic/disease-types/99999", json={"active": True}, headers=h).status_code == 404


def test_legacy_hypertension_leveling_unchanged(client, h, base):
    """存量三病种阈值行为不变：sbp≥160 或 dbp≥100 → 3级。"""
    chronic = client.get("/api/chronic?disease=hypertension", headers=h).json()[0]
    assert client.post(
        f"/api/chronic/{chronic['id']}/followups", json={"sbp": 165, "dbp": 95}, headers=h
    ).json()["level"] == 3
    assert client.post(
        f"/api/chronic/{chronic['id']}/followups", json={"sbp": 145, "dbp": 85}, headers=h
    ).json()["level"] == 2
    assert client.post(
        f"/api/chronic/{chronic['id']}/followups", json={"sbp": 120, "dbp": 78}, headers=h
    ).json()["level"] == 1
    # 单项缺失（require_all）→ 维持原级
    assert client.post(
        f"/api/chronic/{chronic['id']}/followups", json={"sbp": 180}, headers=h
    ).json()["level"] == 1
