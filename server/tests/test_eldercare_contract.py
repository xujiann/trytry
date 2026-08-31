"""老年健康 `/api/eldercare` 三个待治理端点（失能清单/预警/统计）的**特征化网 + 响应契约**。

套路同 test_maternal_contract.py / test_users_contract.py：先钉住**当前**响应的
完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。assessments 两端点已治理（AssessmentOut），仅作种子。

本簇的建模判断（都以此处的精确断言为依据）：

- 本簇无 Money 列；`disabled_rate_pct` 与 `avg_score` 是真除法/`round(...)`
  派生，**有值必 float**、空分支为 null → 声明 `float | None`（键恒在值可空，
  不是条件键，无需 exclude_unset），零分支在造数前单独钉。
- `by_care_level` 键是失能等级中文名、随数据变 → `dict[str, int]`，
  键序=按人（patient_id 升序）首次遇到的等级序，此处逐键钉死。
- 统计口径"按每位老人最近一次评估"（同一人评三次只占一个坑）与
  "认知/体质未做单列"两条业务语义由精确数字钉住，治理不许动。
- 失能清单行三键 / 预警行四键 / 统计八键三种形状，三个模型不互相注入；
  `cognitive` / `tcm_constitution` 是嵌套两/三键子形状，各自建模。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

STATS_KEYS = [
    "assessed_people", "assessment_records", "by_care_level", "disabled_count",
    "disabled_rate_pct", "cognitive", "tcm_constitution", "caliber",
]
CALIBER = "按每位老人最近一次评估统计（非评估条数）；认知筛查与体质辨识未做的单列，不按 0 分并入"


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_统计零分支精确(client, admin):
    """放在最前：无任何评估时，两个比率的 null 分支才钉得住。"""
    body = client.get("/api/eldercare/stats", headers=admin).json()
    assert list(body.keys()) == STATS_KEYS
    assert body == {
        "assessed_people": 0,
        "assessment_records": 0,
        "by_care_level": {},
        "disabled_count": 0,
        "disabled_rate_pct": None,
        "cognitive": {"screened": 0, "unscreened": 0, "avg_score": None},
        "tcm_constitution": {"done": 0, "not_done": 0},
        "caliber": CALIBER,
    }


@pytest.fixture(scope="module")
def seeded(client, admin):
    """elder1 评两次（中度→重度，体质只在**首评**填过）；elder2 一次、能力完好且评估久远。"""
    elder1 = client.post(
        "/api/patients",
        json={"name": "契约长者", "id_card": "330281194501012226", "gender": "女",
              "birth_date": "1945-01-01"},
        headers=admin,
    ).json()
    elder2 = client.post(
        "/api/patients",
        json={"name": "契约长者乙", "id_card": "330281194801013338", "gender": "男",
              "birth_date": "1948-01-01"},
        headers=admin,
    ).json()
    a1 = client.post(
        "/api/eldercare/assessments",
        json={"patient_id": elder1["id"], "adl_score": 55, "cognitive_score": 20,
              "tcm_constitution": "阳虚质", "assessed_date": "2025-06-01"},
        headers=admin,
    )
    assert a1.status_code == 201, a1.text
    a2 = client.post(
        "/api/eldercare/assessments",
        json={"patient_id": elder1["id"], "adl_score": 35, "cognitive_score": 18,
              "assessed_date": "2026-05-01"},
        headers=admin,
    ).json()
    a3 = client.post(
        "/api/eldercare/assessments",
        json={"patient_id": elder2["id"], "adl_score": 100, "assessed_date": "2024-01-15"},
        headers=admin,
    ).json()
    return {"elder1": elder1, "elder2": elder2, "a2": a2, "a3": a3}


def test_失能清单精确_每人取最新一次(client, admin, seeded):
    rows = client.get("/api/eldercare/disabled", headers=admin).json()
    # elder1 首评中度（55）已被复评重度（35）覆盖——只出最新一行；能力完好的 elder2 不出
    assert [list(r.keys()) for r in rows] == [["patient_id", "care_level", "adl_score"]]
    assert rows == [
        {"patient_id": seeded["elder1"]["id"], "care_level": "重度失能", "adl_score": 35}
    ]
    assert type(rows[0]["adl_score"]) is int


def test_预警精确_重度专案与年度复评(client, admin, seeded):
    body = client.get("/api/eldercare/alerts?today=2026-08-31", headers=admin).json()
    assert list(body.keys()) == ["total", "alerts"]
    assert [list(a.keys()) for a in body["alerts"]] == [
        ["patient_id", "alert_type", "message", "assessed_date"]
    ] * 2
    assert body == {
        "total": 2,
        "alerts": [
            {"patient_id": seeded["elder1"]["id"], "alert_type": "severe_disability",
             "message": "重度失能，建议纳入家庭病床/上门服务专案", "assessed_date": "2026-05-01"},
            {"patient_id": seeded["elder2"]["id"], "alert_type": "reassess_due",
             "message": "距上次健康评估已超一年，应安排复评", "assessed_date": "2024-01-15"},
        ],
    }
    assert client.get("/api/eldercare/alerts?today=bad", headers=admin).status_code == 422


def test_统计精确_按人不按条(client, admin, seeded):
    body = client.get("/api/eldercare/stats", headers=admin).json()
    assert list(body.keys()) == STATS_KEYS
    assert list(body["by_care_level"].keys()) == ["重度失能", "能力完好"]  # 按 patient_id 首遇序
    assert body == {
        "assessed_people": 2,        # 两位老人（elder1 两条评估只占一个坑）
        "assessment_records": 3,
        "by_care_level": {"重度失能": 1, "能力完好": 1},
        "disabled_count": 1,
        "disabled_rate_pct": 50.0,
        # elder1 最新一次 cognitive_score=18 计入；elder2 的 0 分算"未筛查"单列
        "cognitive": {"screened": 1, "unscreened": 1, "avg_score": 18.0},
        # 体质辨识只在 elder1 的**首评**填过——最新一次没填，按未做统计
        "tcm_constitution": {"done": 0, "not_done": 2},
        "caliber": CALIBER,
    }
    assert isinstance(body["disabled_rate_pct"], float)
    assert isinstance(body["cognitive"]["avg_score"], float)
