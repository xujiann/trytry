"""块5：上报报表导出——监测指标14项JSON/CSV、运营月报CSV、director/admin权限。"""
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


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def setup(client, admin):
    county = client.post(
        "/api/organizations",
        json={"name": "报表总院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    township = client.post(
        "/api/organizations",
        json={"name": "报表卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "报表患者", "id_card": "330281199404047007"},
        headers=admin,
    ).json()
    users = {}
    for username, role in [("rp_dir", "director"), ("rp_doc", "doctor"), ("rp_op", "operator")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": county["id"]},
            headers=admin,
        )
        users[role] = login(client, username, "pass123456")
    # 业务数据：基层1次+县级1次就诊、1次上转、本地医保结算、两笔财务
    # 用 admin（全域）建档：横向隔离下 county 医生不能以 township 名义建就诊
    for org in (county, township):
        client.post(
            "/api/encounters",
            json={"patient_id": patient["id"], "org_id": org["id"], "diagnosis_name": "上呼吸道感染"},
            headers=admin,
        )
    client.post(
        "/api/referrals",
        json={
            "patient_id": patient["id"],
            "from_org_id": township["id"],
            "to_org_id": county["id"],
            "direction": "up",
            "reason": "报表测试",
        },
        headers=users["doctor"],
    )
    client.post(
        "/api/insurance/settlements",
        json={
            "patient_id": patient["id"],
            "org_id": county["id"],
            "settle_type": "local",
            "total_amount": 1000,
            "insurance_pay": 600,
            "self_pay": 400,
        },
        headers=users["operator"],
    )
    for period, category, amount in [("2026-07", "income", 80000), ("2026-07", "expense", 50000)]:
        client.post(
            "/api/mgmt/finance",
            json={"org_id": county["id"], "period": period, "category": category, "item": "医疗", "amount": amount},
            headers=users["director"],
        )
    return {"county": county, "township": township, "patient": patient, **users}


def test_monitoring_json_14_indicators(client, setup):
    resp = client.get("/api/reports/monitoring", headers=setup["director"])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 14 and len(data["indicators"]) == 14
    for indicator in data["indicators"]:
        assert indicator["name"] and indicator["caliber"] and indicator["source"]
        assert "value" in indicator
    by_name = {i["name"]: i for i in data["indicators"]}
    assert by_name["县域内基层诊疗人次占比"]["value"] == 50.0  # 2次就诊中1次在乡级
    assert by_name["上转人次"]["value"] == 1
    assert by_name["县域内医保基金支出占比"]["value"] == 100.0  # 全部本地结算


def test_monitoring_role_guard(client, setup):
    assert client.get("/api/reports/monitoring", headers=setup["doctor"]).status_code == 403
    assert client.get("/api/reports/monitoring", headers=setup["operator"]).status_code == 403
    assert client.get("/api/reports/monitoring/export", headers=setup["doctor"]).status_code == 403
    assert client.get("/api/reports/operations/export", headers=setup["operator"]).status_code == 403
    assert client.get("/api/reports/monitoring").status_code == 401


def test_monitoring_csv_export(client, setup, admin):
    resp = client.get("/api/reports/monitoring/export", headers=setup["director"])
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "monitoring_indicators.csv" in resp.headers["content-disposition"]
    text = resp.content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["序号", "指标名", "口径", "当期值", "单位", "数据来源"]
    assert len(rows) == 15  # 表头 + 14 项
    names = [r[1] for r in rows[1:]]
    assert "检查检验结果互认率" in names and "住院均次费用" in names
    # admin 亦可导出
    assert client.get("/api/reports/monitoring/export", headers=admin).status_code == 200


def test_operations_csv_export(client, setup):
    resp = client.get("/api/reports/operations/export", headers=setup["director"])
    assert resp.status_code == 200
    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    assert rows[0] == ["机构ID", "机构名称", "层级", "门急诊人次", "住院人次", "收入(元)", "支出(元)", "结余(元)", "绩效评分"]
    by_name = {r[1]: r for r in rows[1:]}
    county = by_name["报表总院"]
    assert county[3] == "1"  # 就诊1次
    # 阶段十二：金额列改 NUMERIC 后整数金额从库里读回是 int，
    # 故导出统一格式化到分——同一列时而带小数点时而不带，比少两位小数麻烦得多
    assert county[5] == "80000.00" and county[6] == "50000.00" and county[7] == "30000.00"
    township = by_name["报表卫生院"]
    assert township[3] == "1" and township[5] == "0.00"


def test_operations_period_filter(client, setup):
    hit = client.get("/api/reports/operations/export?period=2026-07", headers=setup["director"])
    rows = {r[1]: r for r in list(csv.reader(io.StringIO(hit.content.decode("utf-8-sig"))))[1:]}
    assert rows["报表总院"][5] == "80000.00"
    miss = client.get("/api/reports/operations/export?period=2026-06", headers=setup["director"])
    rows = {r[1]: r for r in list(csv.reader(io.StringIO(miss.content.decode("utf-8-sig"))))[1:]}
    assert rows["报表总院"][5] == "0.00"
    # 期间格式校验
    assert (
        client.get("/api/reports/operations/export?period=202607", headers=setup["director"]).status_code
        == 422
    )
