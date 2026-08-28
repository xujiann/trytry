"""统一规则引擎 `/api/rules` 六个端点的**特征化网 + 响应契约**。

套路同 `test_analytics_contract.py`：先钉住**当前**响应的完整 JSON（dict 相等）
与键序 → 再加 `response_model` → 加完逐字节不变（CLAUDE.md §11）。

建模判断：

- `/domains` 的 `sample` 是**三种真实类型并存**（float / str / bool，样例值
  刻意覆盖真实类型，见 DOMAIN_VARIABLES 注释）——声明 `float | str | bool`，
  smart union 逐值原样透传；本文件把三种类型的取值与顺序全部钉死。
- `/catalog` 的五路来源（unified + 四套 legacy）行形状**完全一致**
  （source/engine/domain/key/name/detail/active），不是多态，逐字段建模；
  `detail` 恒为拼好的字符串。
- `total_deduction` 是 Integer 列求和（空集为字面量 `0`），恒 int，不涉 Money。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


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


# ------------------------------------------------------------------- /domains

DOMAINS_EXPECTED = [
    {
        "domain": "exam",
        "variables": [
            {"name": "center_type", "sample": "imaging", "type": "str"},
            {"name": "critical", "sample": False, "type": "bool"},
            {"name": "item_code", "sample": "DR-CHEST", "type": "str"},
            {"name": "turnaround_hours", "sample": 4.0, "type": "float"},
        ],
    },
    {
        "domain": "medical_record",
        "variables": [
            {"name": "chief_complaint", "sample": "咳嗽3天", "type": "str"},
            {"name": "diagnosis_basis", "sample": "结合症状体征", "type": "str"},
            {"name": "past_history", "sample": "无特殊", "type": "str"},
            {"name": "physical_exam", "sample": "双肺呼吸音清", "type": "str"},
            {"name": "present_illness", "sample": "患者3天前受凉后出现咳嗽", "type": "str"},
            {"name": "qc_score", "sample": 100.0, "type": "float"},
            {"name": "treatment_plan", "sample": "对症治疗", "type": "str"},
        ],
    },
    {
        "domain": "org_performance",
        "variables": [
            {"name": "avg_length_of_stay", "sample": 7.5, "type": "float"},
            {"name": "bed_occupancy_rate_pct", "sample": 82.0, "type": "float"},
            {"name": "chronic_patients", "sample": 50.0, "type": "float"},
            {"name": "encounters", "sample": 100.0, "type": "float"},
            {"name": "referrals_down", "sample": 3.0, "type": "float"},
            {"name": "referrals_up", "sample": 5.0, "type": "float"},
        ],
    },
    {
        "domain": "prescription",
        "variables": [
            {"name": "age", "sample": 40.0, "type": "float"},
            {"name": "daily_dose", "sample": 500.0, "type": "float"},
            {"name": "days", "sample": 7.0, "type": "float"},
            {"name": "diagnosis_name", "sample": "2型糖尿病", "type": "str"},
            {"name": "drug_code", "sample": "METFORMIN", "type": "str"},
            {"name": "is_pregnant", "sample": False, "type": "bool"},
            {"name": "max_daily_dose", "sample": 1000.0, "type": "float"},
            {"name": "renal_impairment", "sample": False, "type": "bool"},
        ],
    },
]


def test_domains_全量精确形状与键序(client, admin):
    body = client.get("/api/rules/domains", headers=admin).json()
    assert body == DOMAINS_EXPECTED
    assert list(body[0].keys()) == ["domain", "variables"]
    assert list(body[0]["variables"][0].keys()) == ["name", "sample", "type"]
    # sample 的三种真实类型——`float | str | bool` 建模的**全部依据**：
    # float 不得变 int / bool 不得变 0/1（改字节），此处逐类型钉死
    rx = {v["name"]: v["sample"] for v in body[3]["variables"]}
    assert isinstance(rx["daily_dose"], float) and rx["daily_dose"] == 500.0
    assert isinstance(rx["is_pregnant"], bool) and rx["is_pregnant"] is False
    assert isinstance(rx["drug_code"], str)


# ------------------------------------------------------- 规则录入 / 列表 / 求值

RULE_KEY_ORDER = [
    "id", "key", "name", "domain", "condition", "message", "severity",
    "deduct_points", "active",
]


@pytest.fixture(scope="module")
def seeded_rules(client, admin):
    overdose = client.post(
        "/api/rules",
        json={
            "key": "ct_overdose",
            "name": "契约超量",
            "domain": "prescription",
            "condition": "daily_dose > max_daily_dose",
            "message": "日剂量超过上限",
            "severity": "error",
            "deduct_points": 10,
        },
        headers=admin,
    )
    assert overdose.status_code == 201, overdose.text
    elderly = client.post(
        "/api/rules",
        json={
            "key": "ct_elderly",
            "name": "契约老年提示",
            "domain": "prescription",
            "condition": "age >= 65",
            "severity": "info",
            "deduct_points": 2,
        },
        headers=admin,
    )
    assert elderly.status_code == 201, elderly.text
    return {"overdose": overdose.json(), "elderly": elderly.json()}


def test_录入回执精确形状与键序(seeded_rules):
    body = seeded_rules["overdose"]
    assert list(body.keys()) == RULE_KEY_ORDER
    assert body == {
        "id": body["id"],
        "key": "ct_overdose",
        "name": "契约超量",
        "domain": "prescription",
        "condition": "daily_dose > max_daily_dose",
        "message": "日剂量超过上限",
        "severity": "error",
        "deduct_points": 10,
        "active": True,
    }
    # message 缺省分支：空串（不是 null）
    assert seeded_rules["elderly"] == {
        "id": seeded_rules["elderly"]["id"],
        "key": "ct_elderly",
        "name": "契约老年提示",
        "domain": "prescription",
        "condition": "age >= 65",
        "message": "",
        "severity": "info",
        "deduct_points": 2,
        "active": True,
    }


def test_列表与回执同形(client, admin, seeded_rules):
    rows = client.get("/api/rules", headers=admin).json()
    # 按 key 排序：ct_elderly < ct_overdose
    assert rows == [seeded_rules["elderly"], seeded_rules["overdose"]]
    assert client.get("/api/rules?domain=prescription", headers=admin).json() == rows
    assert client.get("/api/rules?domain=exam", headers=admin).json() == []


EVALUATE_KEY_ORDER = ["domain", "evaluated", "hits", "errors", "total_deduction", "blocked"]
HIT_KEY_ORDER = ["key", "name", "severity", "message", "deduct_points"]


def test_求值_命中与拦截精确(client, admin, seeded_rules):
    resp = client.post(
        "/api/rules/evaluate",
        json={
            "domain": "prescription",
            "variables": {"daily_dose": 3000, "max_daily_dose": 2000, "age": 78},
        },
        headers=admin,
    )
    body = resp.json()
    assert list(body.keys()) == EVALUATE_KEY_ORDER
    assert list(body["hits"][0].keys()) == HIT_KEY_ORDER
    assert body == {
        "domain": "prescription",
        "evaluated": 2,
        "hits": [
            # message 为空的规则回落到 name——两条分支都钉住
            {"key": "ct_elderly", "name": "契约老年提示", "severity": "info",
             "message": "契约老年提示", "deduct_points": 2},
            {"key": "ct_overdose", "name": "契约超量", "severity": "error",
             "message": "日剂量超过上限", "deduct_points": 10},
        ],
        "errors": [],
        "total_deduction": 12,
        "blocked": True,
    }


def test_求值_不拦截分支精确(client, admin, seeded_rules):
    body = client.post(
        "/api/rules/evaluate",
        json={
            "domain": "prescription",
            "variables": {"daily_dose": 100, "max_daily_dose": 2000, "age": 70},
        },
        headers=admin,
    ).json()
    assert body == {
        "domain": "prescription",
        "evaluated": 2,
        "hits": [
            {"key": "ct_elderly", "name": "契约老年提示", "severity": "info",
             "message": "契约老年提示", "deduct_points": 2},
        ],
        "errors": [],
        "total_deduction": 2,
        "blocked": False,
    }


def test_停用回执精确(client, admin, seeded_rules):
    resp = client.delete("/api/rules/ct_elderly", headers=admin)
    assert list(resp.json().keys()) == ["key", "active"]
    assert resp.json() == {"key": "ct_elderly", "active": False}
    after = client.post(
        "/api/rules/evaluate",
        json={"domain": "prescription", "variables": {"age": 70}},
        headers=admin,
    ).json()
    assert after == {
        "domain": "prescription",
        "evaluated": 1,
        "hits": [],
        "errors": [],
        "total_deduction": 0,
        "blocked": False,
    }


# ------------------------------------------------------------------- /catalog

CATALOG_ENTRY_KEY_ORDER = ["source", "engine", "domain", "key", "name", "detail", "active"]


def test_目录_五路来源精确形状与键序(client, admin, seeded_rules):
    body = client.get("/api/rules/catalog", headers=admin).json()
    assert list(body.keys()) == ["total", "by_source", "entries"]
    for entry in body["entries"]:
        assert list(entry.keys()) == CATALOG_ENTRY_KEY_ORDER
    assert body["total"] == len(body["entries"])
    # by_source 与 entries 全量一致（不硬编码药品/绩效种子规模），三路已知来源钉精确值
    counted: dict[str, int] = {}
    for entry in body["entries"]:
        counted[entry["source"]] = counted.get(entry["source"], 0) + 1
    assert body["by_source"] == counted
    assert body["by_source"]["unified"] == 2
    assert body["by_source"]["data_quality"] == 15
    assert body["by_source"]["record_quality"] == 12
    by_key = {(e["source"], e["key"]): e for e in body["entries"]}
    # 统一引擎行：停用状态如实透出（active=False，不是被过滤掉）
    assert by_key[("unified", "ct_elderly")] == {
        "source": "unified", "engine": "unified", "domain": "prescription",
        "key": "ct_elderly", "name": "契约老年提示", "detail": "age >= 65", "active": False,
    }
    assert by_key[("data_quality", "QC001")] == {
        "source": "data_quality", "engine": "legacy", "domain": "data_quality",
        "key": "QC001", "name": "患者身份证号须符合18位格式与校验位",
        "detail": "logic on patients", "active": True,
    }
    assert by_key[("record_quality", "MRQC01")] == {
        "source": "record_quality", "engine": "legacy", "domain": "medical_record",
        "key": "MRQC01", "name": "主诉必填", "detail": "required on chief_complaint",
        "active": True,
    }
