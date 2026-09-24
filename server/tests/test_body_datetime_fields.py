"""请求体时间戳字段走时间戳真源（P1-100）：逐端点回归 + 三个消费方的行为回归。

13 个「某时某分」的请求体字段原先是 `max_length=16/19` 的裸 `str`，界面上是自由文本框。2026-09-24 开发库
实测（修前代码），形状不一的值全部 201 照存，之后：

- **定时宣教**按 `send_at <= 现在`（字符串）派发：「2026-10-1 8:00」10-10 才发（晚 9 天）、「10月1日」当场就发、
  「2026/10/01 08:00」到年底都没发；ISO 的 `2026-10-01T08:00` 晚到 10-02 零点（同一天里 `T` 排在空格之后）。
- **体温单**按测量时刻（字符串）排序：「2026-09-24 8:00」排在「14:00」之后，38.5℃ 的那次画到了退热之后。
- **病理冷缺血**解析不了就当「未采集」：离体到固定 150 分钟的标本，质控统计 `over_60min` 是 0。

修法：`datetypes.DateTimeStr`（到分钟，16 位的列）/ `DateTimeSecStr`（秒可有可无，19 位的列）及其可空版，
收 `YYYY-MM-DD[ HH:MM[:SS]]`（`T` 分隔也收），合法值**原样落库**（出参字节不变，契约用例钉着）；按字符串比较 /
排序的两个消费方（定时宣教派发、体温单）把 `T` 换成空格再比。`test_datestr_single_source.py` 的时间戳闸门盯
「还剩多少」（零基线），本文件钉「改了的确实挡住了、合法的确实放行了、三个消费方确实对了」。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app import clock
from app.database import SessionLocal
from app.spd.jobs import spd_edu_push_dispatch
from app.spd.models import SpdEduPush

#: (路径, 其余字段, 时间戳字段, 是否可空, 是否收秒)。其余字段只求过得了请求体校验，id 一律填不存在的——
#: 请求体校验先于业务查找，422 一定来自时间戳字段。
CASES = [
    ("/api/vaccine-supply/cold-chain", {"org_id": 999999, "device_name": "冷藏柜", "temperature": 5},
     "recorded_at", False, True),
    ("/api/spd/edu-pushes", {"material_id": 999999, "patient_ids": [999999]}, "send_at", True, True),
    ("/api/education/live-sessions", {"title": "高血压讲座"}, "planned_at", True, False),
    ("/api/labqc/lots/999999/measurements", {"value": 5.1}, "measured_at", True, False),
    ("/api/inpatient/admissions/999999/progress-notes", {"note_type": "daily", "content": "病程"},
     "recorded_at", True, False),
    ("/api/inpatient/admissions/999999/nursing-records", {}, "recorded_at", True, False),
    ("/api/inpatient/admissions/999999/vitals", {"temperature": 36.8}, "measured_at", False, False),
    ("/api/outpatient/encounters/999999/nursing-records", {"content": "门诊护理"}, "recorded_at", True, False),
    ("/api/outpatient/encounters/999999/treatments", {"treatment_name": "换药"}, "performed_at", True, False),
    ("/api/pathology/specimens", {"request_id": 999999}, "excised_at", True, True),
    ("/api/pathology/specimens", {"request_id": 999999}, "fixed_at", True, True),
    ("/api/surgery/requests/999999/record", {"actual_surgery_name": "阑尾切除术"}, "start_at", True, False),
    ("/api/surgery/requests/999999/record", {"actual_surgery_name": "阑尾切除术"}, "end_at", True, False),
]

#: 修前全部照存的写法：不补零、中文、斜杠、不存在的日子、不存在的时刻、全角数字
BAD = ["2026-10-1 8:00", "10月1日", "2026/10/01 08:00", "2026-02-30 08:00", "2026-10-01 24:00", "２０２６-10-01 08:00"]


def _case_id(case):
    """`inpatient/admissions/vitals.measured_at`：带上资源路径，同名字段不撞车。"""
    return "/".join(seg for seg in case[0].split("/")[2:] if not seg.isdigit()) + f".{case[2]}"


def _field_errors(resp, field):
    if resp.status_code != 422 or not isinstance(resp.json().get("detail"), list):
        return []
    return [e for e in resp.json()["detail"] if e.get("loc") == ["body", field]]


@pytest.mark.parametrize("path, body, field, optional, seconds", CASES, ids=[_case_id(c) for c in CASES])
def test_写错的时间戳在请求体校验层就被挡下(client, admin, path, body, field, optional, seconds):
    for bad in BAD + ([] if seconds else ["2026-10-01 08:00:30"]):
        resp = client.post(path, json={**body, field: bad}, headers=admin)
        assert _field_errors(resp, field), (path, bad, resp.status_code, resp.text[:200])
    if not optional:
        assert _field_errors(client.post(path, json={**body, field: ""}, headers=admin), field)


@pytest.mark.parametrize("path, body, field, optional, seconds", CASES, ids=[_case_id(c) for c in CASES])
def test_合法时间戳越过这一道校验(client, admin, path, body, field, optional, seconds):
    # 只到日期的也收（原先就收，直播计划时间的契约用例钉着；按字符串比较排在当天一切时刻之前，时序不乱）
    good = ["2026-10-01 08:00", "2026-10-01T08:00", "2026-10-01"] + (["2026-10-01 08:00:30"] if seconds else [])
    if optional:
        good.append("")
    for value in good:
        resp = client.post(path, json={**body, field: value}, headers=admin)
        assert not _field_errors(resp, field), (path, value, resp.text[:200])


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations",
                      json={"name": "时间戳回归县医院", "org_type": "lead_hospital", "level": "county"},
                      headers=admin).json()
    patients = [client.post("/api/patients",
                            json={"name": f"时间戳患者{i}", "id_card": f"33028119880101{i:04d}"},
                            headers=admin).json()["id"] for i in range(3)]
    return {"org": org["id"], "patients": patients}


def _push_status(patient_id):
    db = SessionLocal()
    try:
        (row,) = db.query(SpdEduPush).filter(SpdEduPush.patient_id == patient_id).all()
        return row.send_at, row.status
    finally:
        db.close()


def _dispatch_at(monkeypatch, moment: str):
    monkeypatch.setattr(clock, "now_naive", lambda: datetime.fromisoformat(moment))
    db = SessionLocal()
    try:
        spd_edu_push_dispatch(db)
        db.commit()
    finally:
        db.close()


def test_定时宣教的T写法到点当场派发_而不是等到第二天零点(client, admin, world, monkeypatch):
    material = client.post("/api/spd/edu-materials",
                           json={"code": "p1100_edu", "title": "限盐宣教", "content": "每日盐不超过5克"},
                           headers=admin).json()["id"]
    patient = world["patients"][0]
    for bad in ("2026-10-1 8:00", "10月1日", "2026/10/01 08:00"):
        resp = client.post("/api/spd/edu-pushes",
                           json={"material_id": material, "patient_ids": [patient], "channel": "app", "send_at": bad},
                           headers=admin)
        assert _field_errors(resp, "send_at"), bad
    resp = client.post("/api/spd/edu-pushes",
                       json={"material_id": material, "patient_ids": [patient], "channel": "app",
                             "send_at": "2026-10-01T08:00"},
                       headers=admin)
    assert resp.status_code == 201, resp.text
    assert _push_status(patient) == ("2026-10-01T08:00", "pending")   # 原样落库
    # 换真源之前存进去的 `T` 写法（存量）：派发端一并换成空格再比，同样到点就发
    db = SessionLocal()
    try:
        db.add(SpdEduPush(material_id=material, patient_id=world["patients"][2], channel="app",
                          send_at="2026-10-01T08:00:00", frequency="once", status="pending"))
        db.commit()
    finally:
        db.close()

    _dispatch_at(monkeypatch, "2026-10-01 07:59:59")
    assert _push_status(patient)[1] == "pending"   # 没到点不发
    assert _push_status(world["patients"][2])[1] == "pending"
    _dispatch_at(monkeypatch, "2026-10-01 08:00:30")
    # 到点当场派发（修前：`T` 排在空格之后，要等到 10-02 零点）
    assert _push_status(patient)[1] != "pending"
    assert _push_status(world["patients"][2])[1] != "pending"


def test_体温单按测量时刻排序_T写法与空格写法混录也不乱序(client, admin, world):
    ward = client.post("/api/inpatient/wards", json={"name": "时间戳病区", "org_id": world["org"]},
                       headers=admin).json()
    bed = client.post("/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "TS-1"}, headers=admin).json()
    adm = client.post("/api/inpatient/admissions",
                      json={"patient_id": world["patients"][1], "org_id": world["org"], "ward_id": ward["id"],
                            "bed_id": bed["id"], "doctor_name": "时间戳医生", "diagnosis_name": "肺炎"},
                      headers=admin)
    assert adm.status_code == 201, adm.text
    url = f"/api/inpatient/admissions/{adm.json()['id']}/vitals"
    assert _field_errors(client.post(url, json={"measured_at": "2026-09-24 8:00", "temperature": 38.5},
                                     headers=admin), "measured_at")
    for at, temp in (("2026-09-24T14:00", 37.0), ("2026-09-24 08:00", 38.5), ("2026-09-24T10:30", 37.8)):
        resp = client.post(url, json={"measured_at": at, "temperature": temp}, headers=admin)
        assert resp.status_code == 201, resp.text
    rows = client.get(url, headers=admin).json()
    # 原样落库、按时序排：`T` 写法与空格写法混在同一天也不乱（排序时把 `T` 换成空格再比）
    assert [(r["measured_at"], r["temperature"]) for r in rows] == [
        ("2026-09-24 08:00", 38.5), ("2026-09-24T10:30", 37.8), ("2026-09-24T14:00", 37.0)]


def test_冷缺血时间照常算出_超60分钟的标本计入质控(client, admin, world):
    req = client.post("/api/exams",
                      json={"patient_id": world["patients"][2], "from_org_id": world["org"],
                            "center_type": "pathology", "item_code": "P001", "item_name": "组织病理学检查"},
                      headers=admin)
    assert req.status_code == 201, req.text
    body = {"request_id": req.json()["id"], "site": "胃窦"}
    bad = client.post("/api/pathology/specimens",
                      json={**body, "excised_at": "2026/09/24 08:00:00", "fixed_at": "2026/09/24 10:30:00"},
                      headers=admin)
    assert _field_errors(bad, "excised_at") and _field_errors(bad, "fixed_at")
    ok = client.post("/api/pathology/specimens",
                     json={**body, "excised_at": "2026-09-24T08:00", "fixed_at": "2026-09-24 10:30:00"},
                     headers=admin)
    assert ok.status_code == 201, ok.text
    assert (ok.json()["excised_at"], ok.json()["cold_ischemia_minutes"]) == ("2026-09-24T08:00", 150)
    stats = client.get("/api/pathology/specimen-stats", headers=admin).json()["cold_ischemia"]
    assert stats["measured"] == 1 and stats["over_60min"] == 1, stats
