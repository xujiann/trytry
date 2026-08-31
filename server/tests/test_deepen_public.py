"""深化轮（4-5）：绩效指标目录化、法定传染病目录与迟报清单。"""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import login


@pytest.fixture(scope="module")
def setup(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "目录化卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "目录化患者", "id_card": "320981199202026666"},
        headers=admin,
    ).json()
    roles = {}
    for username, role in [("dl_doc", "doctor"), ("dl_dir", "director")]:
        resp = client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
        assert resp.status_code == 201, resp.text
        roles[role] = login(client, username, "pass123456")
    return {"org": org, "patient": patient, **roles}


# ---------------------------------------------------------------- 4 绩效目录化


def test_indicators_seeded_and_guarded(client, setup):
    # 启动种子化：现有5维权重入表
    rows = client.get("/api/performance/indicators", headers=setup["director"]).json()
    weights = {r["key"]: r["weight"] for r in rows}
    assert weights == {"referral": 20, "remote_exam": 20, "chronic": 25, "rx": 20, "contract": 15}
    assert all(r["active"] for r in rows)
    # 非管理层不可读/调
    assert client.get("/api/performance/indicators", headers=setup["doctor"]).status_code == 403
    assert (
        client.patch(
            "/api/performance/indicators/rx", json={"weight": 30}, headers=setup["doctor"]
        ).status_code
        == 403
    )


def test_weight_adjustment_changes_score(client, admin, setup):
    director = setup["director"]
    # 造 1 条家医履约记录：contract 维度得分率 1/5
    contract = client.post(
        "/api/contracts",
        json={"patient_id": setup["patient"]["id"], "org_id": setup["org"]["id"], "doctor_name": "戴医生"},
        headers=setup["doctor"],
    ).json()
    assert (
        client.post(
            f"/api/contracts/{contract['id']}/services",
            json={"service_type": "visit", "note": "上门随访"},
            headers=setup["doctor"],
        ).status_code
        == 201
    )

    data = client.get("/api/performance/orgs", headers=director).json()
    assert sum(data["weights"].values()) == 100
    card = next(c for c in data["scorecards"] if c["org_id"] == setup["org"]["id"])
    # 默认权重：contract 15 分 × 1/5 = 3.0
    assert card["score"] == 3.0

    # 管理层调权重：仅保留 contract 维度 → 归一化到 100，分数变为 1/5 × 100 = 20.0
    for key in ["referral", "remote_exam", "chronic", "rx"]:
        resp = client.patch(
            f"/api/performance/indicators/{key}", json={"active": False}, headers=director
        )
        assert resp.status_code == 200
    adjusted = client.get("/api/performance/orgs", headers=director).json()
    assert adjusted["weights"] == {"contract": 100.0}
    card2 = next(c for c in adjusted["scorecards"] if c["org_id"] == setup["org"]["id"])
    assert card2["score"] == 20.0

    # 权重不必为100：恢复启用并把 contract 调到 45（总和130），仍按比例归一化
    for key in ["referral", "remote_exam", "chronic", "rx"]:
        client.patch(f"/api/performance/indicators/{key}", json={"active": True}, headers=director)
    patched = client.patch(
        "/api/performance/indicators/contract", json={"weight": 45}, headers=director
    )
    assert patched.status_code == 200 and patched.json()["weight"] == 45
    final = client.get("/api/performance/orgs", headers=director).json()
    assert abs(sum(final["weights"].values()) - 100) < 0.1
    assert final["weights"]["contract"] == round(45 / 130 * 100, 2)

    # 全部停用被拒绝；不存在的指标 404
    for key in ["referral", "remote_exam", "chronic", "rx"]:
        client.patch(f"/api/performance/indicators/{key}", json={"active": False}, headers=director)
    assert (
        client.patch(
            "/api/performance/indicators/contract", json={"active": False}, headers=director
        ).status_code
        == 422
    )
    assert (
        client.patch("/api/performance/indicators/nope", json={"weight": 1}, headers=director).status_code
        == 404
    )


# ---------------------------------------------------------------- 5 传染病目录


def test_disease_directory_seeded(client, admin):
    diseases = client.get("/api/infectious/diseases", headers=admin).json()
    by_code = {d["code"]: d for d in diseases}
    assert by_code["A20"]["name"] == "鼠疫"
    assert by_code["A20"]["category"] == "A" and by_code["A20"]["report_hours"] == 2
    assert by_code["A00"]["category"] == "A" and by_code["A00"]["report_hours"] == 2
    assert by_code["A15"]["category"] == "B" and by_code["A15"]["report_hours"] == 24
    assert by_code["J11"]["category"] == "C" and by_code["J11"]["report_hours"] == 24
    a_only = client.get("/api/infectious/diseases?category=A", headers=admin).json()
    assert {d["code"] for d in a_only} == {"A20", "A00"}


def test_report_case_backfills_category(client, admin, setup):
    doc = setup["doctor"]
    listed = client.post(
        "/api/infectious/cases",
        json={
            "org_id": setup["org"]["id"],
            "disease_code": "A15",
            "disease_name": "肺结核",
            "onset_date": "2026-08-01",
        },
        headers=doc,
    ).json()
    assert listed["category"] == "B"

    unknown = client.post(
        "/api/infectious/cases",
        json={
            "org_id": setup["org"]["id"],
            "disease_code": "ZZZ",
            "disease_name": "目录外病种",
            "onset_date": "2026-08-01",
        },
        headers=doc,
    ).json()
    assert unknown["category"] == ""


def test_late_reports(client, admin, setup):
    from app.database import SessionLocal
    from app.models import InfectiousCase

    doc = setup["doctor"]
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # 甲类鼠疫：发病 3 天前才报 → 迟报（2小时时限）
    plague = client.post(
        "/api/infectious/cases",
        json={
            "org_id": setup["org"]["id"],
            "disease_code": "A20",
            "disease_name": "鼠疫",
            "onset_date": (now - timedelta(days=3)).strftime("%Y-%m-%d"),
        },
        headers=doc,
    ).json()
    # 乙类肺结核：当天报 → 不迟报；另一例相隔 5 天 → 迟报
    ontime = client.post(
        "/api/infectious/cases",
        json={
            "org_id": setup["org"]["id"],
            "disease_code": "A15",
            "disease_name": "肺结核",
            "onset_date": now.strftime("%Y-%m-%d"),
        },
        headers=doc,
    ).json()
    tb_late = client.post(
        "/api/infectious/cases",
        json={
            "org_id": setup["org"]["id"],
            "disease_code": "A15",
            "disease_name": "肺结核",
            "onset_date": (now - timedelta(days=5)).strftime("%Y-%m-%d"),
        },
        headers=doc,
    ).json()

    late = client.get("/api/infectious/late-reports", headers=admin).json()
    late_ids = {r["case_id"] for r in late}
    assert plague["id"] in late_ids
    assert tb_late["id"] in late_ids
    assert ontime["id"] not in late_ids
    by_id = {r["case_id"]: r for r in late}
    assert by_id[plague["id"]]["category"] == "A"
    assert by_id[plague["id"]]["report_hours"] == 2
    assert by_id[plague["id"]]["days_late"] == 3
    assert by_id[tb_late["id"]]["days_late"] == 5

    # 甲类跨日即迟报：把一例鼠疫发病日改为昨天 → 1 天 × 24h > 2h 仍迟报
    db = SessionLocal()
    try:
        row = db.get(InfectiousCase, plague["id"])
        row.onset_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        db.commit()
    finally:
        db.close()
    late2 = client.get("/api/infectious/late-reports", headers=admin).json()
    assert {r["case_id"] for r in late2} >= {plague["id"], tb_late["id"]}
