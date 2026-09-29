"""居民端「可约号源」只拿到全县最早的 200 个号，没有按机构 / 日期筛的入口（P2-376）。

页面调 `/api/portal/me/slots` 不带参数：全县各机构的号按日期、时段排，拿最早的 200 个——机构一多，后面几天的号、
某家医院的门诊在手机上看不到、也约不上。清单接口早就能按 `org_id` / `slot_date` 筛，页面没有入口；机构下拉又没有
现成的居民端来源。

修法：新增 `/api/portal/me/slot-orgs`（有可约号源的机构，口径与清单一致：今天及以后、还有余号的），页面加机构下拉
与日期，按它们重拉清单；拿满一页时提示去筛。
"""
from datetime import timedelta
from pathlib import Path

import pytest

from app.database import SessionLocal
from conftest import business_today

SRC = (Path(__file__).resolve().parents[1] / "app" / "static" / "m" / "m.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def world(client, admin):
    from app.models import AppointmentSlot, SmsCode

    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": f"P2376 {name}", "org_type": "township", "level": "township"}).json()["id"] for name in ("甲院", "乙院")]
    today = business_today()   # 清单按业务日期判「今天及以后」
    with SessionLocal() as db:
        db.add_all([
            AppointmentSlot(org_id=orgs[0], resource_type="doctor", resource_name="甲院全科",
                            slot_date=(today + timedelta(days=1)).isoformat(), slot_time="08:00", capacity=5, booked=0),
            AppointmentSlot(org_id=orgs[0], resource_type="doctor", resource_name="甲院全科（约满）",
                            slot_date=(today + timedelta(days=1)).isoformat(), slot_time="09:00", capacity=1, booked=1),
            AppointmentSlot(org_id=orgs[1], resource_type="doctor", resource_name="乙院内科",
                            slot_date=(today + timedelta(days=2)).isoformat(), slot_time="08:00", capacity=5, booked=0),
            AppointmentSlot(org_id=orgs[1], resource_type="doctor", resource_name="乙院内科（已过）",
                            slot_date=(today - timedelta(days=1)).isoformat(), slot_time="08:00", capacity=5, booked=0),
        ])
        db.query(SmsCode).filter(SmsCode.phone == "13800042376").delete()
        db.commit()
    client.post("/api/patients", headers=admin, json={
        "name": "P2376 居民", "id_card": "330102198501012376", "phone": "13800042376"})
    code = client.post("/api/portal/auth/sms/code", json={"phone": "13800042376", "purpose": "login"}).json()
    login = client.post("/api/portal/auth/sms/login", json={"phone": "13800042376", "code": code["debug_code"]})
    assert login.status_code == 200, login.text
    return {"orgs": orgs, "headers": {"Authorization": f"Bearer {login.json()['access_token']}"}, "today": today}


def test_有可约号源的机构_口径与清单一致(client, world):
    got = client.get("/api/portal/me/slot-orgs", headers=world["headers"])
    assert got.status_code == 200, got.text
    mine = {r["org_id"]: (r["org_name"], r["available"]) for r in got.json() if r["org_id"] in world["orgs"]}
    # 约满的、已过期的不算
    assert mine == {world["orgs"][0]: ("P2376 甲院", 1), world["orgs"][1]: ("P2376 乙院", 1)}
    assert "X-Total-Count" in got.headers


def test_机构下拉印的是剩余名额之和_不是时段条数(client, world):
    """P2-850（第二十三批「部分之和 vs 整体」扫描 Y4-7）：下拉原先印 `available`（还有余号的时段条数），同一页每条的
    「余号」是剩余名额——各机构这里都是 1 个时段、5 个号，修前下拉印「1 个号」。"""
    got = client.get("/api/portal/me/slot-orgs", headers=world["headers"]).json()
    mine = {r["org_id"]: (r["available"], r["remaining"]) for r in got if r["org_id"] in world["orgs"]}
    assert mine == {world["orgs"][0]: (1, 5), world["orgs"][1]: (1, 5)}   # 约满的那个时段不算余量
    start = SRC.index("async function renderAppointments(box)")
    body = SRC[start:SRC.index("\nasync function renderContracts", start)]
    assert "（余 ${o.remaining} 个号）" in body and "${o.available} 个号" not in body


def test_清单按机构与日期筛(client, world):
    tomorrow = (world["today"] + timedelta(days=1)).isoformat()
    rows = client.get("/api/portal/me/slots", headers=world["headers"],
                      params={"org_id": world["orgs"][1]}).json()
    assert [r["resource_name"] for r in rows] == ["乙院内科"]
    rows = client.get("/api/portal/me/slots", headers=world["headers"], params={"slot_date": tomorrow}).json()
    assert {r["resource_name"] for r in rows} >= {"甲院全科"} and "乙院内科" not in {r["resource_name"] for r in rows}


def test_未登录不给看(client):
    assert client.get("/api/portal/me/slot-orgs").status_code == 401


def test_页面有机构与日期筛选():
    start = SRC.index("async function renderAppointments(box)")
    body = SRC[start:SRC.index("\nasync function renderContracts", start)]
    assert "/api/portal/me/slot-orgs" in body and 'id="slot-org"' in body and 'id="slot-date"' in body, body
    assert 'qs.set("org_id"' in body and 'qs.set("slot_date"' in body   # 修前 authApi("/api/portal/me/slots") 不带参数
