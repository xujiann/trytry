"""上报报表 `/api/reports` 三个端点的**特征化网 + 响应契约**。

本模块的特殊之处：三个端点分两类，契约的形态也不同。

- `/monitoring` 是 JSON → Pydantic 契约。`value` 字段必须是 `int | float`：
  计数类指标（机构数/建档数/例数）是 int，比率与均次费用是 `round(...)` 出来的
  float。声明成 float 会把 `12` 变成 `12.0` —— 改字节。
- 两个 `*/export` 直接返回 `StreamingResponse`（CSV 字节流），`response_model`
  对它们没有意义。`printing` 那批 HTML 单据的 `response_model=str` 在这里**不适用**：
  那些端点真的返回 str 再由 `HTMLResponse` 渲染，这里返回的是 Response 对象本身。
  改为在 `responses` 里显式声明 `text/csv`，OpenAPI 里就真的写着"返回 CSV"。
  棘轮据此把它们算作已声明契约（判据从路由推导，见
  `test_api_contract_governance._declares_non_json_media`）。
"""
import csv
import io

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
def director(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def seeded(client, director):
    """造出**同时含计数与比率**的指标值——只有计数时 int/float 的区分测不出来。"""
    org = client.post("/api/organizations",
                      json={"name": "上报契约院", "org_type": "township", "level": "township"},
                      headers=director).json()
    patient = client.post("/api/patients",
                          json={"name": "上报契约患者", "id_card": "330281199001014512"},
                          headers=director).json()
    client.post("/api/encounters",
                json={"patient_id": patient["id"], "org_id": org["id"],
                      "diagnosis_name": "高血压", "diagnosis_code": "I10"},
                headers=director)
    return {"org_id": org["id"], "patient_id": patient["id"]}


INDICATOR_KEYS = {"no", "name", "caliber", "value", "unit", "source"}


def test_monitoring_键集合与类型(client, director, seeded):
    body = client.get("/api/reports/monitoring", headers=director).json()
    assert set(body) == {"generated_at", "total", "indicators"}
    assert isinstance(body["total"], int)
    assert isinstance(body["generated_at"], str)
    assert len(body["indicators"]) == body["total"]
    for item in body["indicators"]:
        assert set(item) == INDICATOR_KEYS
        assert isinstance(item["no"], int)
        for key in ("name", "caliber", "unit", "source"):
            assert isinstance(item[key], str) and item[key], f"{key} 为空"
        assert isinstance(item["value"], (int, float)) and not isinstance(item["value"], bool)


def test_指标值的int与float都真实出现过(client, director, seeded):
    """`value` 建成 `int | float` 的依据：14 项指标里两种类型并存。

    计数类（机构数/建档数/例数）是 int，比率与均次费用是 round 出来的 float。
    写成 float 会把 `12` 变成 `12.0`——与 analytics 那批同一个陷阱。
    """
    body = client.get("/api/reports/monitoring", headers=director).json()
    kinds = {type(i["value"]).__name__ for i in body["indicators"]}
    assert kinds == {"int", "float"}, (
        f"实测到的指标值类型是 {kinds}——两种都要出现，否则 int|float 的建模依据不成立"
    )


def test_monitoring_十四项且序号连续(client, director, seeded):
    body = client.get("/api/reports/monitoring", headers=director).json()
    assert body["total"] == 14, "监测指标体系（2024版）平台可算的是 14 项"
    assert [i["no"] for i in body["indicators"]] == list(range(1, 15))


# ------------------------------------------------------------ CSV 下载两端点
@pytest.mark.parametrize("url,filename", [
    ("/api/reports/monitoring/export", "monitoring_indicators.csv"),
    ("/api/reports/operations/export", "operations_report_all.csv"),
])
def test_csv导出仍是附件下载_契约没把它变成JSON(client, director, seeded, url, filename):
    """加 `responses` 声明**不能**改变实际响应——它只写进 OpenAPI。"""
    resp = client.get(url, headers=director)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert f'filename="{filename}"' in resp.headers["content-disposition"]
    assert resp.content.startswith("﻿".encode()), "BOM 丢了，Excel 打开会乱码"
    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    assert len(rows) >= 2 and rows[0], "至少有表头与一行数据"


def test_openapi里两个导出端点写明了text_csv(client):
    """这才是本次给它们"声明契约"的实际内容——OpenAPI 里真的写着返回 CSV。

    没有这一条，棘轮把它们算作已治理就成了空头承诺。
    """
    schema = client.get("/openapi.json").json()
    for path in ("/api/reports/monitoring/export", "/api/reports/operations/export"):
        content = schema["paths"][path]["get"]["responses"]["200"]["content"]
        # 媒体类型带 charset（`text/csv; charset=utf-8`），按前缀判
        assert any(ct.startswith("text/csv") for ct in content), (
            f"{path} 的 200 响应没写明 text/csv：{list(content)}"
        )
        assert not any(ct.startswith("application/json") for ct in content), (
            f"{path} 仍被标成可能返回 JSON：{list(content)}——"
            "只写 responses={200:{content:{...}}} 时 FastAPI 会保留默认的 JSON 条目，"
            "必须让 response_class 自带 media_type 才能把它去掉"
        )


def test_monitoring的json端点没被误标成csv(client):
    """防呆：别把媒体类型声明抄到不该抄的端点上。"""
    schema = client.get("/openapi.json").json()
    content = schema["paths"]["/api/reports/monitoring"]["get"]["responses"]["200"]["content"]
    assert "application/json" in content and "text/csv" not in content
