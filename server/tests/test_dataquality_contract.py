"""数据质控 `/api/dataquality` 六个端点的**特征化网 + 响应契约**。

套路同 `test_analytics_contract.py`：先钉住**当前**响应的完整 JSON（dict 相等）
与键序 → 再加 `response_model` → 加完逐字节不变（CLAUDE.md §11）。

场景经 HTTP 种：空库先跑一遍 `/run` 钉住零分支，再建一名**缺出生日期**的患者
（身份证校验位正确，只触发 QC003 一条 warn），使 `/run` 与 `/summary` 的全量
输出可静态写死——15 条种子规则逐条列出，`by_rule` 是完整精确比对而非抽查。

建模判断：本模块没有 Money/Float 列出参，全部计数为 int；`config` 是 JSON 列
（结构随 rule_type 而异，见种子文件），照 `workflows.nodes` 的先例用
`dict[str, Any]` 宽字典透传。
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


RULE_KEY_ORDER = [
    "id", "code", "name", "target_table", "rule_type", "rule_type_name",
    "config", "severity", "severity_name", "active",
]
RUN_KEY_ORDER = ["total", "error_total", "warn_total", "offset", "limit", "items"]
VIOLATION_KEY_ORDER = [
    "rule_code", "rule_name", "rule_type", "severity", "table", "record_id", "message",
]

#: 15 条种子规则的 (code, name, target_table, rule_type, severity)——
#: `/summary.by_rule` 的完整期望由它生成（种子幂等且"已存在编码不覆盖"，取值稳定）。
SEED_RULES = [
    ("QC001", "患者身份证号须符合18位格式与校验位", "patients", "logic", "error"),
    ("QC002", "患者姓名不得为空", "patients", "required", "error"),
    ("QC003", "患者出生日期宜完整填写", "patients", "required", "warn"),
    ("QC004", "就诊记录须填写诊断编码", "encounters", "required", "error"),
    ("QC005", "就诊诊断编码须在 ICD-10 诊断字典内", "encounters", "cross_ref", "error"),
    ("QC006", "检查检验报告须有诊断结论", "exam_reports", "required", "error"),
    ("QC007", "危急值报告须闭环至处置反馈", "exam_reports", "logic", "warn"),
    ("QC008", "处方药品日剂量须大于 0", "prescription_items", "range", "error"),
    ("QC009", "处方用药天数须在 1—90 天之间", "prescription_items", "range", "warn"),
    ("QC010", "处方须填写临床诊断", "prescriptions", "required", "warn"),
    ("QC011", "慢病档案须有下次随访到期日", "chronic_patients", "required", "warn"),
    ("QC012", "慢病病种须在病种目录内", "chronic_patients", "cross_ref", "error"),
    ("QC013", "慢病随访须记录对应病种指标", "followups", "logic", "warn"),
    ("QC014", "住院出院时间不得早于入院时间", "admissions", "logic", "error"),
    ("QC015", "传染病报告发病日期不得晚于当日", "infectious_cases", "logic", "error"),
]
RULE_TYPE_NAMES = {
    "required": "必填项", "range": "数值区间", "enum": "取值枚举",
    "cross_ref": "引用校验", "logic": "逻辑校验",
}


def test_规则清单_种子行精确形状与键序(client, admin):
    rows = client.get("/api/dataquality/rules", headers=admin).json()
    assert len(rows) == 15
    for row in rows:
        assert list(row.keys()) == RULE_KEY_ORDER
    by_code = {r["code"]: r for r in rows}
    assert by_code["QC001"] == {
        "id": by_code["QC001"]["id"],
        "code": "QC001",
        "name": "患者身份证号须符合18位格式与校验位",
        "target_table": "patients",
        "rule_type": "logic",
        "rule_type_name": "逻辑校验",
        "config": {"check": "id_card_checksum", "field": "id_card"},
        "severity": "error",
        "severity_name": "错误",
        "active": True,
    }
    assert by_code["QC003"] == {
        "id": by_code["QC003"]["id"],
        "code": "QC003",
        "name": "患者出生日期宜完整填写",
        "target_table": "patients",
        "rule_type": "required",
        "rule_type_name": "必填项",
        "config": {"field": "birth_date"},
        "severity": "warn",
        "severity_name": "警告",
        "active": True,
    }


def test_空库run全零精确(client, admin):
    resp = client.get("/api/dataquality/run", headers=admin)
    assert list(resp.json().keys()) == RUN_KEY_ORDER
    assert resp.json() == {
        "total": 0, "error_total": 0, "warn_total": 0, "offset": 0, "limit": 200, "items": [],
    }
    assert resp.headers["X-Total-Count"] == "0"


@pytest.fixture(scope="module")
def flawed_patient(client, admin):
    """身份证校验位正确、缺出生日期：全库唯一一条违规（QC003，warn）。"""
    return client.post(
        "/api/patients",
        json={"name": "质控契约患者", "id_card": "330281199203046014"},
        headers=admin,
    ).json()


def test_违规检出_run精确(client, admin, flawed_patient):
    expected_item = {
        "rule_code": "QC003",
        "rule_name": "患者出生日期宜完整填写",
        "rule_type": "required",
        "severity": "warn",
        "table": "patients",
        "record_id": flawed_patient["id"],
        "message": "birth_date 为空",
    }
    resp = client.get("/api/dataquality/run", headers=admin)
    assert list(resp.json()["items"][0].keys()) == VIOLATION_KEY_ORDER
    assert resp.json() == {
        "total": 1, "error_total": 0, "warn_total": 1, "offset": 0, "limit": 200,
        "items": [expected_item],
    }
    # 过滤与分页参数回显
    assert client.get("/api/dataquality/run?rule_code=QC003&limit=1", headers=admin).json() == {
        "total": 1, "error_total": 0, "warn_total": 1, "offset": 0, "limit": 1,
        "items": [expected_item],
    }
    assert client.get("/api/dataquality/run?severity=error", headers=admin).json() == {
        "total": 0, "error_total": 0, "warn_total": 0, "offset": 0, "limit": 200, "items": [],
    }


def test_汇总精确形状与键序(client, admin, flawed_patient):
    resp = client.get("/api/dataquality/summary", headers=admin)
    body = resp.json()
    assert list(body.keys()) == ["rules_checked", "total", "by_severity", "by_table", "by_rule"]
    assert list(body["by_rule"][0].keys()) == [
        "rule_code", "rule_name", "rule_type", "rule_type_name", "table", "severity", "violations"
    ]
    assert body == {
        "rules_checked": 15,
        "total": 1,
        "by_severity": {"error": 0, "warn": 1},
        "by_table": {
            "patients": 1, "encounters": 0, "exam_reports": 0, "prescription_items": 0,
            "prescriptions": 0, "chronic_patients": 0, "followups": 0, "admissions": 0,
            "infectious_cases": 0,
        },
        "by_rule": [
            {
                "rule_code": code,
                "rule_name": name,
                "rule_type": rule_type,
                "rule_type_name": RULE_TYPE_NAMES[rule_type],
                "table": table,
                "severity": severity,
                "violations": 1 if code == "QC003" else 0,
            }
            for code, name, table, rule_type, severity in SEED_RULES
        ],
    }


def test_规则CRUD回执精确(client, admin):
    created = client.post(
        "/api/dataquality/rules",
        json={
            "code": "QCT90",
            "name": "证明类型须在枚举内",
            "target_table": "medical_certs",
            "rule_type": "enum",
            "config": {"field": "cert_type", "values": ["birth", "death"]},
            "severity": "warn",
        },
        headers=admin,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert list(body.keys()) == RULE_KEY_ORDER
    assert body == {
        "id": body["id"],
        "code": "QCT90",
        "name": "证明类型须在枚举内",
        "target_table": "medical_certs",
        "rule_type": "enum",
        "rule_type_name": "取值枚举",
        "config": {"field": "cert_type", "values": ["birth", "death"]},
        "severity": "warn",
        "severity_name": "警告",
        "active": True,
    }
    patched = client.patch(
        f"/api/dataquality/rules/{body['id']}", json={"severity": "error"}, headers=admin
    ).json()
    assert patched == {**body, "severity": "error", "severity_name": "错误"}
    deleted = client.delete(f"/api/dataquality/rules/{body['id']}", headers=admin)
    assert list(deleted.json().keys()) == ["deleted"]
    assert deleted.json() == {"deleted": body["id"]}
