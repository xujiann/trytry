"""管理端号源清单不带日期时只列今天及以后（P2-882，第二十四批「时间窗口的边界」扫描 Z1-1）。

`GET /api/appointments/slots` 不带 `slot_date` 时没有日期下界、按日期正序分页（缺省 500 条）；管理端预约页不带参数调它，
「预约」表单要从这张表里抄号源 ID。同一张号源表的其余出口——资源目录（P2-164）、居民端（P2-64）、寻医——都只列今天及
以后。开诊一两个月后面板拿回的 500 行全是过去的号，明天的号不在里面，照表抄 ID 去约只得 409「该号源日期已过」。
修后不带日期从今天起；带日期的等值查询不变。
"""
from datetime import timedelta

from app.database import SessionLocal
from app.models import AppointmentSlot
from conftest import business_today


def test_不带日期只列今天及以后_带日期照查(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2882 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    today = business_today()
    with SessionLocal() as db:
        past = [AppointmentSlot(org_id=org, resource_type="outpatient", resource_name="P2882 内科",
                                slot_date=(today - timedelta(days=d)).isoformat(), slot_time=f"{8 + n:02d}:00",
                                capacity=5, booked=0)
                for d in range(1, 61) for n in range(10)]   # 过去 60 天、每天 10 个号
        tomorrow = AppointmentSlot(org_id=org, resource_type="outpatient", resource_name="P2882 内科",
                                   slot_date=(today + timedelta(days=1)).isoformat(), slot_time="08:00",
                                   capacity=5, booked=0)
        db.add_all(past + [tomorrow])
        db.commit()
        tomorrow_id, past_date = tomorrow.id, past[0].slot_date
    rows = client.get("/api/appointments/slots", headers=admin, params={"org_id": org}).json()
    assert [r["id"] for r in rows] == [tomorrow_id]   # 修前 500 行全是过去的号，明天的不在
    same_day = client.get("/api/appointments/slots", headers=admin, params={"org_id": org, "slot_date": past_date})
    assert len(same_day.json()) == 10   # 带日期的照查（含过去的日子）
