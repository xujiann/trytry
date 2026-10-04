"""慢专病指标趋势的 `granularity` 写错静默回落成按日，回显的却是传进来的值（P2-1337，第三十九批「趋势、环比与同比」扫描 AC1-5）。

`GET /api/spd/measurements/trend` 的 `granularity` 原先是裸 `str`：`month` / `week` 以外的值一律落进按日的分支——
`granularity=weekly`、`Month` 都是 200，points 是 `2025-12-28 / 2025-12-29 / …` 一天一个点，出参却回显 `'weekly'`、
`'Month'`，调用方以为拿到的是按周 / 按月的点。修后只收 day / week / month，写错 422；三种合法值的行为不变（页面与既有
用例都不带这个参数，缺省按日）。

同一条里还订正了两处说明（不改行为，OpenAPI 的接口说明取自它们）：居民端 `GET /api/portal/spd/measurements` 原写「前端
自行按日/周/月切换展示」，居民端「监测」页只列最新 30 条原始读数；本接口原写「患者端 #7」，它是员工端接口，居民令牌调不了。
"""
import re
from datetime import timedelta

import pytest

from app.clock import now_naive
from app.database import SessionLocal

TREND = "/api/spd/measurements/trend"


@pytest.fixture(scope="module")
def patient(client, admin):
    from app.spd.models import SpdMeasurement

    pid = client.post("/api/patients", headers=admin, json={
        "name": "P21337 患者", "id_card": "330106197001011337", "gender": "男", "birth_date": "1970-01-01"}).json()["id"]
    now = now_naive()
    with SessionLocal() as db:
        for days_ago, value in ((40, 150), (9, 146), (8, 144), (1, 140)):
            db.add(SpdMeasurement(patient_id=pid, metric="bp_sys", value=value, unit="mmHg",
                                  measured_at=now - timedelta(days=days_ago)))
        db.commit()
    return pid


@pytest.mark.parametrize("granularity", ["weekly", "Month", "year"])
def test_写错的粒度422(client, admin, patient, granularity):
    got = client.get(TREND, headers=admin, params={"patient_id": patient, "metric": "bp_sys", "granularity": granularity})
    assert got.status_code == 422, got.text   # 修前 200：按日出点，granularity 回显传进来的值
    assert got.json()["detail"][0]["loc"] == ["query", "granularity"]


@pytest.mark.parametrize("granularity, label", [
    ("day", r"[0-9]{4}-[0-9]{2}-[0-9]{2}"), ("week", r"[0-9]{4}-W[0-9]{2}"), ("month", r"[0-9]{4}-[0-9]{2}")])
def test_三种合法粒度照旧(client, admin, patient, granularity, label):
    got = client.get(TREND, headers=admin, params={"patient_id": patient, "metric": "bp_sys", "granularity": granularity})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["granularity"] == granularity and body["total"] == 4
    assert body["points"] and all(re.fullmatch(label, p["label"]) for p in body["points"]), body["points"]


def test_不带粒度按日(client, admin, patient):
    body = client.get(TREND, headers=admin, params={"patient_id": patient, "metric": "bp_sys"}).json()
    assert body["granularity"] == "day" and len(body["points"]) == 4   # 页面「看趋势」就是这么调的


def test_两处说明照实写():
    from app.spd.routers import care, portal

    resident = portal.list_measurements.__doc__ or ""
    # 修前「指标历史与趋势（#7）。按日返回原始点，前端自行按日/周/月切换展示。」
    assert "趋势" not in resident.splitlines()[0] and "原始读数" in resident and "未交付" in resident, resident
    staff = care.measurement_trend.__doc__ or ""
    assert "患者端" not in staff.splitlines()[0] and "员工端" in staff, staff   # 修前首行「指标趋势（患者端 #7）」
