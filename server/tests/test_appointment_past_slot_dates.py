"""放号、批量排班不收早于业务日的日期；寻医的起始日期填过去，下界仍取今天（P2-1301，第三十八批扫描 AB2-9）。

修前：放号（`create_slot`）的 `slot_date` 只校验形状，批量排班（`batch_create_slots`）的区间没有下界——这样建出来的号
管理端清单不列（P2-882）、谁也约不上（P2-64）；寻医（`find_doctors`）直接拿 `from_date` 当下界，过去的号算作可约。
开发库实测：年份敲成去年的单条放号 201；批量区间从 10 天前开始得到 `{'created': 13, …}`，过去的 10 天也计进了生成数，
号源面板却只列出 3 条；寻医 `from_date` 填 10 天前，`available_slots = 13`、`bookable = True`、`next_slots` 从 10 天前
开始，照它给的第一个号去约 409「该号源日期已过」。

修法：单条放号日期早于业务日 422（与约号同一口径「该号源日期已过」）；批量只生成今天及以后，回执末尾加
`skipped_past_dates` 报没生成的已过日期数（`created` / `skipped` 语义不变），页面回执照印；寻医下界取
max(from_date, 今天)。
"""
from datetime import timedelta
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import AppointmentSlot
from conftest import business_today
from jssrc import strip_comments

CORE = strip_comments((Path(__file__).resolve().parents[1] / "app" / "static" / "core.js").read_text(encoding="utf-8"))


def _day(offset: int) -> str:
    return (business_today() + timedelta(days=offset)).isoformat()


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P21301 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]


def _doctor(client, admin, org, name):
    resp = client.post("/api/mgmt/employees", headers=admin, json={"org_id": org, "name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _slots_of(org, resource_name):
    with SessionLocal() as db:
        return sorted(s.slot_date for s in db.query(AppointmentSlot).filter(
            AppointmentSlot.org_id == org, AppointmentSlot.resource_name == resource_name))


def test_单条放号_日期早于业务日422_今天照放(client, admin, org):
    doctor = _doctor(client, admin, org, "P21301 王主任")
    body = {"org_id": org, "resource_type": "outpatient", "resource_name": "P21301 专家门诊", "employee_id": doctor,
            "slot_time": "09:00-10:00", "capacity": 20}
    today = business_today()
    last_year = (today - timedelta(days=366)).isoformat()   # 年份敲错一类：去年的这几天
    for day in (last_year, _day(-1)):
        resp = client.post("/api/appointments/slots", headers=admin, json=dict(body, slot_date=day))
        assert resp.status_code == 422, (day, resp.text)                                  # 修前 201
        assert resp.json()["detail"] == "该号源日期已过，不能放号", resp.text
    assert client.post("/api/appointments/slots", headers=admin, json=dict(body, slot_date=_day(0))).status_code == 201
    assert _slots_of(org, "P21301 专家门诊") == [_day(0)]


def test_批量区间跨过去与将来_只生成今天及以后_回执报跳过的已过日期数(client, admin, org):
    doctor = _doctor(client, admin, org, "P21301 李主任")
    body = {"org_id": org, "date_from": _day(-10), "date_to": _day(2),
            "templates": [{"resource_type": "outpatient", "resource_name": "P21301 下午门诊", "employee_id": doctor,
                           "slot_time": "14:00-15:00", "capacity": 10}]}
    got = client.post("/api/appointments/slots/batch", headers=admin, json=body)
    assert (got.status_code, got.json()) == (201, {"created": 3, "skipped": 0, "skipped_past_dates": 10}), got.text   # 修前 created 13
    assert _slots_of(org, "P21301 下午门诊") == [_day(0), _day(1), _day(2)]
    again = client.post("/api/appointments/slots/batch", headers=admin, json=body)   # 幂等重跑：已有的照旧算 skipped
    assert again.json() == {"created": 0, "skipped": 3, "skipped_past_dates": 10}
    # 整段都在过去：一个不生成，照样回 201 并报出天数（跳过的周末不算「已过日期」——本来就不生成）
    past = client.post("/api/appointments/slots/batch", headers=admin, json=dict(
        body, date_from=_day(-14), date_to=_day(-8), skip_weekends=True))
    assert past.status_code == 201 and past.json() == {"created": 0, "skipped": 0, "skipped_past_dates": 5}, past.text


def test_寻医起始日期填过去_下界仍取今天(client, admin, org):
    doctor = _doctor(client, admin, org, "P21301 赵主任")
    only_past = _doctor(client, admin, org, "P21301 钱主任")
    with SessionLocal() as db:   # 过去的号直接落库：放号接口已不收（见上）；库里的过去号源来自日子过去了的存量
        db.add_all([AppointmentSlot(org_id=org, resource_type="outpatient", resource_name=f"P21301 {who}门诊",
                                    employee_id=emp, slot_date=_day(offset), slot_time="08:00-09:00", capacity=5)
                    for who, emp in (("赵", doctor), ("钱", only_past)) for offset in (-10, -1)])
        db.commit()
    future = client.post("/api/appointments/slots", headers=admin, json={
        "org_id": org, "resource_type": "outpatient", "resource_name": "P21301 赵门诊", "employee_id": doctor,
        "slot_date": _day(1), "slot_time": "08:00-09:00", "capacity": 5})
    assert future.status_code == 201, future.text

    rows = {r["employee_id"]: r for r in client.get("/api/appointments/doctors", headers=admin, params={
        "keyword": "P21301", "org_id": org, "from_date": _day(-10)}).json()}
    zhao, qian = rows[doctor], rows[only_past]
    assert [s["slot_date"] for s in zhao["next_slots"]] == [_day(1)]   # 修前从 10 天前开始：[-10, -1, +1]
    assert zhao["available_slots"] == 1                                # 修前 3
    assert (qian["bookable"], qian["available_slots"], qian["next_slots"]) == (False, 0, [])   # 修前 True、2
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21301 患者", "id_card": "330782198701011301"}).json()["id"]
    booked = client.post("/api/appointments", headers=admin, json={
        "slot_id": zhao["next_slots"][0]["slot_id"], "patient_id": patient})
    assert booked.status_code == 201, booked.text                      # 修前照第一个号去约 409「该号源日期已过」
    # 起始日期在将来的照旧从那天起
    later = client.get("/api/appointments/doctors", headers=admin, params={
        "keyword": "P21301 赵主任", "org_id": org, "from_date": _day(2)}).json()
    assert later[0]["next_slots"] == []


def test_批量回执页面照印已过日期没生成的天数():
    start = CORE.index('$("#slot-batch-form").onsubmit')
    body = CORE[start:CORE.index('$("#doctor-form").onsubmit', start)]
    assert "r.skipped_past_dates" in body   # 修前回执没有这一项
