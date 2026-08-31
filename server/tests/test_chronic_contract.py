"""慢病管理 `/api/chronic` 平台侧 5 个未治理端点的**特征化网 + 响应契约**。

套路同 `test_billing_contract.py` / `test_esb_contract.py`：先钉住**当前**响应的
完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §7/§11）。已治理的 4 个端点（建档/列表/超期/随访列表）不在此列。

本簇的建模判断（都以此处的精确断言为依据）：

- **`level_rules` 是宽 dict 透传**（workflows.nodes 先例）：分级规则的 JSON 结构
  由目录数据决定（metrics 数组、require_all、以及任何自定义键），逐字段建模会把
  自定义键静默滤掉、把阈值 `540`（int）coerce 成 `540.0`——种子里刻意混铺
  int 阈值（hypertension 160）与 float 阈值（diabetes 10.0）各钉一遍。
- **随访数值恒 float**：`sbp`/`dbp`/`glucose` 是 Float 列，`metrics` 入参经
  `dict[str, float]` 校验——整数入参 170 落库读回都是 `170.0`，声明 float 才是
  原样（与 Money 列相反，判据是列类型不是字段名）。`risk.recent_values` 的两条
  产地（Float 列、metrics JSON）也因此恒 float → `list[float]`。
- **`risk.score` 恒 int**：基础分表 + 修正值全是 int 字面量，max/min 不改型；
  声明成 float 会把 `95` 印成 `95.0`。
- 随访回执的 6 键与 risk 的 9 键**全部恒在**（无条件键），不需要 exclude_unset；
  `followup.glucose` 是「键恒在值可空」→ 沿用 FollowUpOut 的 `float | None`。
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

DISEASE_TYPE_KEYS = [
    "id", "code", "name", "level_rules", "guidance", "followup_interval_days", "active",
]
FOLLOWUP_KEYS = ["sbp", "dbp", "glucose", "metrics", "guidance", "next_due", "id", "chronic_id"]
FOLLOWUP_RESULT_KEYS = [
    "followup", "level", "guidance_points", "next_due", "next_due_suggested", "refer_up_suggested",
]
RISK_KEYS = [
    "chronic_id", "disease", "level", "metric", "recent_values",
    "trend", "score", "risk_level", "refer_up_suggested",
]

#: 种子目录里高血压的指导要点（app/chronic_seed.py）——guidance_points 的唯一产地
HYPERTENSION_GUIDANCE = "限盐（每日<5g）、控制体重、戒烟限酒、每周≥150分钟中等强度运动、规律服药并自测血压"

#: 自定义键 + int/float 阈值混铺：level_rules 必须原样透传（宽 dict 建模的全部依据）
GOUT_RULES = {
    "require_all": False,
    "metrics": [
        {"key": "uric_acid", "name": "血尿酸", "unit": "μmol/L",
         "direction": "high", "level3": 540, "level2": 420.5},
    ],
    "备注": "自定义键要原样透传",
}


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
    """一次种完全部场景，测试只做断言（billing 契约网同款布局）。

    随访取值刻意铺满分级三档：患者1 先 170/95（3级）再 135/85（1级，趋势下降）；
    患者2 先 150/80（2级）再 165/88（3级，趋势上升）；糖尿病档案零随访
    （risk 走 insufficient_data 分支）。
    """
    data: dict = {}
    org = client.post(
        "/api/organizations",
        json={"name": "契约慢病医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    data["org"] = org
    client.post(
        "/api/users",
        json={"username": "chct_doc", "password": "pass123456", "role": "doctor", "org_id": org["id"]},
        headers=admin,
    )
    data["doctor"] = login(client, "chct_doc", "pass123456")
    data["p1"] = client.post(
        "/api/patients",
        json={"name": "契约慢病患者一", "id_card": "330881199001018801"},
        headers=admin,
    ).json()
    data["p2"] = client.post(
        "/api/patients",
        json={"name": "契约慢病患者二", "id_card": "330881199001018802"},
        headers=admin,
    ).json()

    # 病种目录：新建（带宽 dict 规则）→ 改名改周期；再建一个专供停用过滤
    resp = client.post(
        "/api/chronic/disease-types",
        json={
            "code": "ct_gout", "name": "痛风(契约)", "level_rules": GOUT_RULES,
            "guidance": "低嘌呤饮食、限酒、多饮水", "followup_interval_days": 30,
        },
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    data["dt_created"] = resp.json()
    data["dt_patched"] = client.patch(
        f"/api/chronic/disease-types/{data['dt_created']['id']}",
        json={"name": "痛风(契约改)", "followup_interval_days": 60},
        headers=admin,
    ).json()
    data["dt_retired"] = client.post(
        "/api/chronic/disease-types",
        json={"code": "ct_retired", "name": "退役病种(契约)"},
        headers=admin,
    ).json()
    data["dt_retired_off"] = client.patch(
        f"/api/chronic/disease-types/{data['dt_retired']['id']}",
        json={"active": False},
        headers=admin,
    ).json()

    def register(patient, disease):
        resp = client.post(
            "/api/chronic",
            json={"patient_id": patient["id"], "disease": disease,
                  "managed_by_org_id": org["id"]},
            headers=data["doctor"],
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    def followup(chronic, payload):
        resp = client.post(
            f"/api/chronic/{chronic['id']}/followups", json=payload, headers=data["doctor"]
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    data["c1"] = register(data["p1"], "hypertension")
    data["c2"] = register(data["p1"], "diabetes")
    data["c3"] = register(data["p2"], "hypertension")
    # 建议到期日 = 业务当天 + 病种周期（高血压 90 天）；随访紧挨着算，跨零点概率可忽略
    data["expected_due"] = (date.today() + timedelta(days=90)).isoformat()
    data["fu1"] = followup(data["c1"], {"sbp": 170, "dbp": 95, "metrics": {"adherence_score": 4}})
    data["fu2"] = followup(data["c1"], {"sbp": 135, "dbp": 85, "next_due": "2027-01-01"})
    data["fu3"] = followup(data["c3"], {"sbp": 150, "dbp": 80})
    data["fu4"] = followup(data["c3"], {"sbp": 165, "dbp": 88})
    return data


# ---------------------------------------------------------------- 病种目录


def test_病种目录新建回执精确_宽字典规则原样透传(seed):
    body = seed["dt_created"]
    assert list(body.keys()) == DISEASE_TYPE_KEYS
    assert body == {
        "id": body["id"],
        "code": "ct_gout",
        "name": "痛风(契约)",
        "level_rules": GOUT_RULES,
        "guidance": "低嘌呤饮食、限酒、多饮水",
        "followup_interval_days": 30,
        "active": True,
    }
    # 宽 dict 不许改值类型：int 阈值不得变 540.0，float 阈值原样
    metric = body["level_rules"]["metrics"][0]
    assert type(metric["level3"]) is int and metric["level3"] == 540
    assert type(metric["level2"]) is float and metric["level2"] == 420.5
    # 自定义键必须原样保留——契约滤掉它即破坏字节
    assert body["level_rules"]["备注"] == "自定义键要原样透传"


def test_病种目录修改回执与新建同形(seed):
    body = seed["dt_patched"]
    assert list(body.keys()) == DISEASE_TYPE_KEYS
    assert body == {**seed["dt_created"], "name": "痛风(契约改)", "followup_interval_days": 60}
    # 缺省值路径：只给 code/name 时规则空 dict、指导空串、周期 90
    retired = seed["dt_retired"]
    assert retired == {
        "id": retired["id"], "code": "ct_retired", "name": "退役病种(契约)",
        "level_rules": {}, "guidance": "", "followup_interval_days": 90, "active": True,
    }
    assert seed["dt_retired_off"] == {**retired, "active": False}


def test_病种目录列表_种子行精确与过滤(client, admin, seed):
    rows = client.get("/api/chronic/disease-types", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [DISEASE_TYPE_KEYS] * len(rows)
    # id 升序：8 个种子病种在前，本簇新建的两个在最后
    assert rows[-2:] == [seed["dt_patched"], seed["dt_retired_off"]]
    by_code = {r["code"]: r for r in rows}
    hyp = by_code["hypertension"]
    assert hyp == {
        "id": hyp["id"],
        "code": "hypertension",
        "name": "高血压",
        "level_rules": {
            "require_all": True,
            "metrics": [
                {"key": "sbp", "name": "收缩压", "unit": "mmHg",
                 "direction": "high", "level3": 160, "level2": 140},
                {"key": "dbp", "name": "舒张压", "unit": "mmHg",
                 "direction": "high", "level3": 100, "level2": 90},
            ],
        },
        "guidance": HYPERTENSION_GUIDANCE,
        "followup_interval_days": 90,
        "active": True,
    }
    # 种子 JSON 的 int/float 之别经宽 dict 原样出参（160 不是 160.0；10.0 不是 10）
    assert type(hyp["level_rules"]["metrics"][0]["level3"]) is int
    dia_metric = by_code["diabetes"]["level_rules"]["metrics"][0]
    assert dia_metric["level3"] == 10.0 and type(dia_metric["level3"]) is float
    assert client.get("/api/chronic/disease-types?active=false", headers=admin).json() == [
        seed["dt_retired_off"]
    ]
    assert client.get("/api/chronic/disease-types?active=true", headers=admin).json() == [
        r for r in rows if r["active"]
    ]


# ---------------------------------------------------------------- 随访回执


def test_随访回执精确_键序与自动建议到期日(seed):
    body = seed["fu1"]
    assert list(body.keys()) == FOLLOWUP_RESULT_KEYS
    assert list(body["followup"].keys()) == FOLLOWUP_KEYS
    assert body == {
        "followup": {
            "sbp": 170.0,
            "dbp": 95.0,
            "glucose": None,
            "metrics": {"adherence_score": 4.0},
            "guidance": "",
            "next_due": seed["expected_due"],
            "id": body["followup"]["id"],
            "chronic_id": seed["c1"]["id"],
        },
        "level": 3,
        "guidance_points": HYPERTENSION_GUIDANCE,
        "next_due": seed["expected_due"],
        "next_due_suggested": True,
        "refer_up_suggested": True,
    }
    # Float 列与 dict[str, float] 校验：整数入参 170/4 读回都是 float（170.0/4.0）
    assert type(body["followup"]["sbp"]) is float
    assert type(body["followup"]["metrics"]["adherence_score"]) is float
    assert type(body["level"]) is int


def test_随访回执精确_显式到期日不再建议(seed):
    body = seed["fu2"]
    assert body == {
        "followup": {
            "sbp": 135.0,
            "dbp": 85.0,
            "glucose": None,
            "metrics": {},
            "guidance": "",
            "next_due": "2027-01-01",
            "id": body["followup"]["id"],
            "chronic_id": seed["c1"]["id"],
        },
        "level": 1,
        "guidance_points": HYPERTENSION_GUIDANCE,
        "next_due": "2027-01-01",
        "next_due_suggested": False,
        "refer_up_suggested": False,
    }


# ---------------------------------------------------------------- 风险评分


def test_风险评分精确_下降与数据不足分支(client, admin, seed):
    body = client.get(f"/api/chronic/{seed['c1']['id']}/risk", headers=admin).json()
    assert list(body.keys()) == RISK_KEYS
    assert body == {
        "chronic_id": seed["c1"]["id"],
        "disease": "hypertension",
        "level": 1,
        "metric": "sbp",
        "recent_values": [170.0, 135.0],
        "trend": "falling",
        "score": 10,
        "risk_level": "low",
        "refer_up_suggested": False,
    }
    # 分级基础分 + 修正全是 int 字面量：score 恒 int（声明 float 会把 10 变 10.0）
    assert type(body["score"]) is int
    assert all(type(v) is float for v in body["recent_values"])

    empty = client.get(f"/api/chronic/{seed['c2']['id']}/risk", headers=admin).json()
    assert empty == {
        "chronic_id": seed["c2"]["id"],
        "disease": "diabetes",
        "level": 1,
        "metric": "glucose",
        "recent_values": [],
        "trend": "insufficient_data",
        "score": 20,
        "risk_level": "low",
        "refer_up_suggested": False,
    }


def test_风险评分精确_上升高危分支(client, admin, seed):
    body = client.get(f"/api/chronic/{seed['c3']['id']}/risk", headers=admin).json()
    assert body == {
        "chronic_id": seed["c3"]["id"],
        "disease": "hypertension",
        "level": 3,
        "metric": "sbp",
        "recent_values": [150.0, 165.0],
        "trend": "rising",
        "score": 95,
        "risk_level": "high",
        "refer_up_suggested": True,
    }
    assert type(body["score"]) is int


# ---------------------------------------------------------------- 错误体


def test_各类错误体都只有detail(client, admin, seed):
    cases = [
        client.post("/api/chronic/disease-types",
                    json={"code": "ct_gout", "name": "重复编码"}, headers=admin),  # 409
        client.patch("/api/chronic/disease-types/999999", json={"name": "无"}, headers=admin),  # 404
        client.post("/api/chronic",
                    json={"patient_id": seed["p1"]["id"], "disease": "ct_retired",
                          "managed_by_org_id": seed["org"]["id"]},
                    headers=seed["doctor"]),  # 停用病种 422
        client.post("/api/chronic/999999/followups", json={"sbp": 120},
                    headers=seed["doctor"]),  # 404
        client.get("/api/chronic/999999/risk", headers=admin),  # 404
    ]
    assert [r.status_code for r in cases] == [409, 404, 422, 404, 404]
    for r in cases:
        assert set(r.json()) == {"detail"}
