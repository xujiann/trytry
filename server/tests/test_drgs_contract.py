"""DRGs 分析 `/api/drgs` 全部 6 个端点的**特征化网 + 响应契约**。

套路同 test_billing_contract.py / test_maternal_contract.py：先钉住**当前**
响应的完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §7/§11）。

本簇的建模判断（都以此处的精确断言为依据）：

- `base_weight` 是**无量纲 Float 列**（不是 Money）：入参经 `float` 字段、落库
  REAL、读回 float——整数入参 `2` 也以 `2.0` 出参（PATCH 用例显式钉住），
  声明 float 才是原样，写 `int | float` 在此没有意义。
- `/stats` 与 `/in-stay-alerts` 的数值派生全是**浮点产地**：`avg_cost` 出自
  SQL AVG（SQLite 恒 REAL，整数均值也是 `9000.0`）、`cmi`/`*_pct`/
  `baseline_avg_days`/`over_ratio` 是真除法 + 兜底字面量 `0.0`——恒 float；
  `cases`/`grouped`/`fallback`/`stayed_days`/`baseline_cases` 恒 int。
- `weight_range` 是「键恒在值可空」（未命中时 null）→ `X | None`，不是条件键；
  本簇没有条件键，无需 exclude_unset。
- 候选组行 = 分组目录行 + 恒在尾键 `match_score`（继承加尾键保键序；
  其 `diagnosis_hits` 实为总分、`procedure_hits` 实为最长命中词长——名字与
  语义的错位是存量出参，契约照原样钉，不趁机改）。
- 事中预警的基线要有正 LOS 才能压出 alerts 行——入出院时刻由服务端落笔，
  HTTP 种不出跨天住院，故种完后用 SessionLocal 把时刻**改到固定日期**、
  `today` 参数也传同一基准日（analytics/metrics 契约网的既有做法），
  三条分支（alerts / insufficient_baseline / ungrouped）各钉一遍。
"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import Admission

GROUP_KEYS = [
    "id", "code", "name", "base_weight", "keywords", "mdc", "mdc_name",
    "procedure_keywords", "require_procedure", "is_fallback", "active",
]
CANDIDATE_KEYS = GROUP_KEYS + ["match_score"]
PRECHECK_KEYS = ["diagnosis", "operation", "matched", "candidates", "weight_range", "caliber"]
ORG_STAT_KEYS = [
    "org_id", "org_name", "cases", "grouped", "fallback",
    "grouped_pct", "fallback_pct", "cmi", "avg_cost",
]
GROUP_STAT_KEYS = ["drg_code", "drg_name", "mdc", "fallback", "cases", "avg_cost"]
MDC_STAT_KEYS = ["mdc", "mdc_name", "groups", "cases", "cmi", "avg_cost", "fallback"]
ALERTS_KEYS = [
    "today", "los_multiplier", "alerts", "insufficient_baseline", "ungrouped_in_stay", "caliber",
]
ALERT_ROW_KEYS = [
    "admission_id", "patient_id", "org_id", "drg_code",
    "stayed_days", "baseline_avg_days", "baseline_cases", "over_ratio",
]
INSUFFICIENT_ROW_KEYS = ["admission_id", "drg_code", "history_cases", "stayed_days"]
PRECHECK_CALIBER = (
    "事前提示给候选组而非结论——入院时诊断未定，报一个确定的组"
    "会让人照着组去写诊断；未命中不落兜底组，那在事前没有信息量"
)
ALERTS_CALIBER = (
    "基线取本院已出院且已入组病例的住院日（由入出院时刻现算）；"
    "同组历史少于 5 例不预警，单列在 "
    "insufficient_baseline；尚未填病案首页的在院病例计入 ungrouped_in_stay"
)

# 事中预警的固定基准日：所有入出院时刻回填到它附近，today 也传它，跑哪天都一样
REF_DAY = "2026-08-15"


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
def seed(client, admin):
    """自建 MDCZ 两组（普通 + 外科 require_procedure）钉目录与事前提示；
    住院病例 9 例钉统计与事中预警：5 例出院 ES31（LOS 10 天做基线）、
    1 例出院 QY 兜底、在院 ES31（住 20 天，超基线 2 倍）/BR23（同组史不足）/
    未填首页各 1 例。"""
    data: dict = {}
    org = client.post(
        "/api/organizations",
        json={"name": "契约DRG医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    data["org"] = org
    client.post(
        "/api/users",
        json={"username": "drgct_dir", "password": "pass123456", "role": "director",
              "org_id": org["id"]},
        headers=admin,
    )
    data["director"] = login(client, "drgct_dir", "pass123456")

    # ---- 分组目录：普通组（整数入参调权）+ 外科组（require_procedure） ----
    resp = client.post(
        "/api/drgs/groups",
        json={"code": "ZK99", "name": "契约试验组", "base_weight": 1.25,
              "keywords": "契约试验病", "mdc": "MDCZ", "mdc_name": "契约大类",
              "procedure_keywords": "契约缝合术"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    data["g1"] = resp.json()
    data["g1_patched"] = client.patch(
        f"/api/drgs/groups/{data['g1']['id']}",
        json={"base_weight": 2, "name": "契约试验组改"},
        headers=admin,
    ).json()
    data["g2"] = client.post(
        "/api/drgs/groups",
        json={"code": "ZK98", "name": "契约外科组", "base_weight": 3.5,
              "keywords": "契约试验病", "mdc": "MDCZ", "mdc_name": "契约大类",
              "procedure_keywords": "契约切除术", "require_procedure": True},
        headers=admin,
    ).json()

    # ---- 病例：5 出院 ES31 + 1 出院 QY + 在院 ES31/BR23/未入组 ----
    ward = client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "契约DRG病区"}, headers=admin
    ).json()
    beds = [
        client.post(
            "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": f"DR-{i}"}, headers=admin
        ).json()
        for i in range(9)
    ]
    patients = [
        client.post(
            "/api/patients",
            json={"name": f"契约DRG患者{i}", "id_card": f"33088119900101{8301 + i:04d}"},
            headers=admin,
        ).json()
        for i in range(9)
    ]

    def admit(i, diagnosis):
        return client.post(
            "/api/inpatient/admissions",
            json={"patient_id": patients[i]["id"], "ward_id": ward["id"],
                  "bed_id": beds[i]["id"], "diagnosis_name": diagnosis},
            headers=admin,
        ).json()

    def summary(adm, diagnosis, cost):
        resp = client.post(
            f"/api/inpatient/admissions/{adm['id']}/case-summary",
            json={"discharge_diagnosis": diagnosis, "total_cost": cost, "outcome": "好转"},
            headers=admin,
        )
        assert resp.status_code in (200, 201), resp.text

    discharged_ids = []
    for i, cost in enumerate([5000, 5500, 6000, 6500, 7000]):
        adm = admit(i, "社区获得性肺炎")
        summary(adm, "社区获得性肺炎", cost)
        client.post(f"/api/inpatient/admissions/{adm['id']}/discharge", headers=admin)
        discharged_ids.append(adm["id"])
    qy = admit(5, "罕见代谢病")
    summary(qy, "罕见代谢病", 3000)
    client.post(f"/api/inpatient/admissions/{qy['id']}/discharge", headers=admin)
    data["stay"] = admit(6, "社区获得性肺炎")
    summary(data["stay"], "社区获得性肺炎", 8000)
    data["rare"] = admit(7, "急性脑梗死")
    summary(data["rare"], "急性脑梗死", 9000)
    data["ungrouped"] = admit(8, "待查")
    data["patients"] = patients

    # 入出院时刻回填到固定基准日（HTTP 种不出跨天住院；today 同传 REF_DAY）：
    # 基线 5 例 LOS=10 天；QY 例当日出院 LOS=0；在院 ES31 到基准日已住 20 天；
    # BR23/未入组在基准日当天入院。
    with SessionLocal() as db:
        for aid in discharged_ids:
            row = db.get(Admission, aid)
            row.admitted_at = datetime(2026, 8, 1, 8, 0, 0)
            row.discharged_at = datetime(2026, 8, 11, 8, 0, 0)
        row = db.get(Admission, qy["id"])
        row.admitted_at = datetime(2026, 8, 9, 8, 0, 0)
        row.discharged_at = datetime(2026, 8, 9, 20, 0, 0)
        db.get(Admission, data["stay"]["id"]).admitted_at = datetime(2026, 7, 26, 8, 0, 0)
        db.get(Admission, data["rare"]["id"]).admitted_at = datetime(2026, 8, 15, 6, 0, 0)
        db.get(Admission, data["ungrouped"]["id"]).admitted_at = datetime(2026, 8, 15, 6, 0, 0)
        db.commit()
    return data


def test_分组目录回执精确_Float权重整数入参也是浮点(client, admin, seed):
    body = seed["g1"]
    assert list(body.keys()) == GROUP_KEYS
    assert body == {
        "id": body["id"], "code": "ZK99", "name": "契约试验组", "base_weight": 1.25,
        "keywords": "契约试验病", "mdc": "MDCZ", "mdc_name": "契约大类",
        "procedure_keywords": "契约缝合术", "require_procedure": False,
        "is_fallback": False, "active": True,
    }
    # Float 列：PATCH 传整数 2，读回就是 2.0（与 Money 相反，float 才是原样）
    patched = seed["g1_patched"]
    assert patched == {**body, "name": "契约试验组改", "base_weight": 2.0}
    assert isinstance(patched["base_weight"], float)


def test_分组目录列表与回执同形_mdc过滤(client, admin, seed):
    rows = client.get("/api/drgs/groups?mdc=MDCZ", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [GROUP_KEYS] * 2
    assert rows == [seed["g2"], seed["g1_patched"]]  # code 升序：ZK98 < ZK99
    all_rows = client.get("/api/drgs/groups", headers=admin).json()
    assert seed["g2"] in all_rows and len(all_rows) > 60  # 种子 62 组 + QY + 自建 2 组
    assert list(all_rows[0].keys()) == GROUP_KEYS


def test_事前提示精确_外科组未报手术不入候选(client, admin, seed):
    resp = client.post(
        "/api/drgs/pre-check", json={"diagnosis": "契约试验病"}, headers=admin
    )
    body = resp.json()
    assert list(body.keys()) == PRECHECK_KEYS
    assert [list(c.keys()) for c in body["candidates"]] == [CANDIDATE_KEYS]
    assert body == {
        "diagnosis": "契约试验病",
        "operation": "",
        "matched": True,
        # ZK98 是 require_procedure 外科组，未报手术不得入候选；
        # match_score 两键：diagnosis_hits 实为总分 10+5、procedure_hits 实为最长词长
        "candidates": [{**seed["g1_patched"], "match_score": {"diagnosis_hits": 15, "procedure_hits": 5}}],
        "weight_range": {"min": 2.0, "max": 2.0},
        "caliber": PRECHECK_CALIBER,
    }
    assert isinstance(body["weight_range"]["min"], float)
    assert type(body["candidates"][0]["match_score"]["diagnosis_hits"]) is int


def test_事前提示命中手术与未命中两分支(client, admin, seed):
    hit = client.post(
        "/api/drgs/pre-check",
        json={"diagnosis": "契约试验病", "operation": "行契约切除术"},
        headers=admin,
    ).json()
    assert hit == {
        "diagnosis": "契约试验病",
        "operation": "行契约切除术",
        "matched": True,
        "candidates": [
            {**seed["g2"], "match_score": {"diagnosis_hits": 35, "procedure_hits": 5}},
            {**seed["g1_patched"], "match_score": {"diagnosis_hits": 15, "procedure_hits": 5}},
        ],
        "weight_range": {"min": 2.0, "max": 3.5},
        "caliber": PRECHECK_CALIBER,
    }
    miss = client.post(
        "/api/drgs/pre-check", json={"diagnosis": "查无此病XYZ"}, headers=admin
    ).json()
    # weight_range 是「键恒在值可空」：未命中时为 null，不是键消失
    assert list(miss.keys()) == PRECHECK_KEYS
    assert miss == {
        "diagnosis": "查无此病XYZ",
        "operation": "",
        "matched": False,
        "candidates": [],
        "weight_range": None,
        "caliber": PRECHECK_CALIBER,
    }


def test_DRG统计精确_三段键序与浮点口径(client, seed):
    resp = client.get("/api/drgs/stats", headers=seed["director"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == ["orgs", "groups", "mdcs"]
    assert [list(r.keys()) for r in body["orgs"]] == [ORG_STAT_KEYS]
    assert [list(r.keys()) for r in body["groups"]] == [GROUP_STAT_KEYS] * 3
    assert [list(r.keys()) for r in body["mdcs"]] == [MDC_STAT_KEYS] * 3
    assert body == {
        # 8 例有首页（5 出院 ES31 + QY + 在院 ES31/BR23），未填首页的不进分母
        "orgs": [{
            "org_id": seed["org"]["id"], "org_name": "契约DRG医院",
            "cases": 8, "grouped": 7, "fallback": 1,
            "grouped_pct": 87.5, "fallback_pct": 12.5,
            # CMI = (0.95×6 + 1.35) / 7，QY 兜底组不进分子分母
            "cmi": 1.007, "avg_cost": 6250.0,
        }],
        "groups": [
            {"drg_code": "BR23", "drg_name": "脑血管疾病", "mdc": "MDCB",
             "fallback": False, "cases": 1, "avg_cost": 9000.0},
            {"drg_code": "ES31", "drg_name": "呼吸系统感染（肺炎）", "mdc": "MDCE",
             "fallback": False, "cases": 6, "avg_cost": 6333.33},
            {"drg_code": "QY", "drg_name": "未入组（歧义组，需病案首页复核）", "mdc": "QY",
             "fallback": True, "cases": 1, "avg_cost": 3000.0},
        ],
        "mdcs": [
            {"mdc": "MDCB", "mdc_name": "神经系统疾病及功能障碍", "groups": 1, "cases": 1,
             "cmi": 1.35, "avg_cost": 9000.0, "fallback": False},
            {"mdc": "MDCE", "mdc_name": "呼吸系统疾病及功能障碍", "groups": 1, "cases": 6,
             "cmi": 0.95, "avg_cost": 6333.33, "fallback": False},
            {"mdc": "QY", "mdc_name": "未入组/歧义", "groups": 1, "cases": 1,
             "cmi": 0.5, "avg_cost": 3000.0, "fallback": True},
        ],
    }
    org_row = body["orgs"][0]
    # SQL AVG 恒 REAL：整数均值也是 9000.0；比率/CMI 是真除法——都恒 float
    assert isinstance(org_row["avg_cost"], float) and isinstance(org_row["cmi"], float)
    assert isinstance(body["groups"][0]["avg_cost"], float)
    assert type(org_row["cases"]) is int and type(org_row["grouped"]) is int
    assert type(org_row["fallback"]) is int


def test_事中预警精确_三条分支各钉一遍(client, admin, seed):
    resp = client.get(f"/api/drgs/in-stay-alerts?today={REF_DAY}", headers=admin)
    body = resp.json()
    assert list(body.keys()) == ALERTS_KEYS
    assert [list(r.keys()) for r in body["alerts"]] == [ALERT_ROW_KEYS]
    assert [list(r.keys()) for r in body["insufficient_baseline"]] == [INSUFFICIENT_ROW_KEYS]
    assert body == {
        "today": REF_DAY,
        "los_multiplier": 1.5,
        # 在院 ES31 已住 20 天，基线 5 例均值 10.0 → 超 2 倍
        "alerts": [{
            "admission_id": seed["stay"]["id"],
            "patient_id": seed["patients"][6]["id"],
            "org_id": seed["org"]["id"],
            "drg_code": "ES31",
            "stayed_days": 20,
            "baseline_avg_days": 10.0,
            "baseline_cases": 5,
            "over_ratio": 2.0,
        }],
        # BR23 同组出院史 0 例：不预警但单列；未填首页的计数报出
        "insufficient_baseline": [{
            "admission_id": seed["rare"]["id"], "drg_code": "BR23",
            "history_cases": 0, "stayed_days": 0,
        }],
        "ungrouped_in_stay": 1,
        "caliber": ALERTS_CALIBER,
    }
    alert = body["alerts"][0]
    assert isinstance(alert["baseline_avg_days"], float) and isinstance(alert["over_ratio"], float)
    assert type(alert["stayed_days"]) is int and type(alert["baseline_cases"]) is int
    assert isinstance(body["los_multiplier"], float)


def test_事中预警_调高倍数后无预警但样本不足仍单列(client, admin, seed):
    body = client.get(
        f"/api/drgs/in-stay-alerts?today={REF_DAY}&los_multiplier=3&org_id={seed['org']['id']}",
        headers=admin,
    ).json()
    assert body["los_multiplier"] == 3.0 and isinstance(body["los_multiplier"], float)
    assert body["alerts"] == []
    assert body["insufficient_baseline"] == [{
        "admission_id": seed["rare"]["id"], "drg_code": "BR23",
        "history_cases": 0, "stayed_days": 0,
    }]
    assert body["ungrouped_in_stay"] == 1


def test_各类错误体都只有detail(client, admin, seed):
    cases = [
        client.post("/api/drgs/groups",
                    json={"code": "ZK99", "name": "重复编码", "base_weight": 1.0},
                    headers=admin),  # 409
        client.patch("/api/drgs/groups/999999", json={"base_weight": 1.0}, headers=admin),  # 404
        client.post("/api/drgs/pre-check", json={"operation": "缺诊断"}, headers=admin),  # 422
        client.get("/api/drgs/in-stay-alerts?los_multiplier=9", headers=admin),  # 超上限 422
        client.get("/api/drgs/in-stay-alerts?today=2026-13-01", headers=admin),  # 日期格式 422
    ]
    assert [r.status_code for r in cases] == [409, 404, 422, 422, 422]
    for r in cases:
        assert set(r.json()) == {"detail"}
