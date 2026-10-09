"""健康日历带宣教（P2-1606，第四十七批「慢专病服务域」扫描 AK2-10）。

`GET /api/spd/health-calendar` 的说明与需求对照表（智能随访端 #12）都写「某天的随访、宣教与复诊安排」，出参却只有
`followups / revisits / tasks` 三段。修前实测（scan47 ak2/r3）：约在 10-20 09:00 的定时宣教，10-20 的日历返回的键是
`['day', 'followups', 'revisits', 'tasks']`，没有宣教。

修法：出参末尾追加 `edu` 一段——计划推送时刻（`send_at`）落在这一天的宣教推送，每项带 id、素材标题、计划时刻、状态
（状态码，页面按宣教推送清单同一张映射出中文，同兄弟段）；前三段字节不变。`send_at` 存的是**本地**时刻字符串
（P2-215；`T` 与空格两种写法都收，P1-100），按本地日直接比——本地 07:30 这种换成 UTC 落在前一天的，归本地那一天。
"""
import os
import time

import pytest

B = "/api/spd"
PAGE = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "pages-spd.js"), encoding="utf-8").read()


@pytest.fixture
def east_eight(monkeypatch):
    """进程时区切到东八区：本地 07:30 换成 UTC 落在前一天，按 UTC 区间比的写法在这里才露馅（同 test_dataquality_timestamp_local_day）。"""
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        yield
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.fixture(scope="module")
def world(client, admin):
    patients = []
    for i in range(2):
        resp = client.post("/api/patients", headers=admin, json={
            "name": f"P21606 患者{i}", "id_card": f"33010619600101{1606 + i:04d}"})
        assert resp.status_code in (200, 201), resp.text
        patients.append(resp.json()["id"])
    material = client.post(f"{B}/edu-materials", headers=admin, json={
        "code": "p21606_salt", "title": "P21606 低盐饮食", "content": "每日食盐不超过 5 克", "program_code": "hypertension"})
    assert material.status_code == 201, material.text

    def push(patient_id: int, send_at: str) -> None:
        resp = client.post(f"{B}/edu-pushes", headers=admin, json={
            "material_id": material.json()["id"], "patient_ids": [patient_id], "channel": "sms", "send_at": send_at})
        assert resp.status_code == 201 and resp.json()["pushed"] == 1, resp.text

    push(patients[0], "2026-10-20 09:00:00")
    push(patients[0], "2026-10-20T23:59")       # 同一天的最后一刻（`T` 写法）
    push(patients[0], "2026-10-21T07:30")       # 本地 07:30：东八区换成 UTC 是前一天 23:30
    push(patients[0], "2026-10-22")             # 只写日期
    push(patients[1], "2026-10-20 09:00:00")    # 别的患者同一天
    return {"patients": patients, "title": material.json()["title"]}


def _calendar(client, admin, patient_id: int, day: str) -> dict:
    resp = client.get(f"{B}/health-calendar", headers=admin, params={"patient_id": patient_id, "day": day})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _edu(client, admin, patient_id: int, day: str) -> list[tuple[str, str, str]]:
    return [(e["title"], e["send_at"], e["status"]) for e in _calendar(client, admin, patient_id, day)["edu"]]


def test_约在这一天的定时宣教出现在这一天的日历(client, admin, world):
    cal = _calendar(client, admin, world["patients"][0], "2026-10-20")
    assert list(cal.keys()) == ["day", "followups", "revisits", "tasks", "edu"]   # 修前没有 edu，前三段原样在前
    assert [list(e.keys()) for e in cal["edu"]] == [["id", "title", "send_at", "status"]] * 2
    assert [(e["title"], e["send_at"], e["status"]) for e in cal["edu"]] == [
        (world["title"], "2026-10-20 09:00:00", "pending"), (world["title"], "2026-10-20T23:59", "pending")]


def test_别的日子不带(client, admin, world):
    assert _edu(client, admin, world["patients"][0], "2026-10-19") == []
    assert [send_at for _, send_at, _ in _edu(client, admin, world["patients"][0], "2026-10-21")] == ["2026-10-21T07:30"]
    assert [send_at for _, send_at, _ in _edu(client, admin, world["patients"][0], "2026-10-22")] == ["2026-10-22"]


def test_本地清早的宣教归本地那一天_不挪到UTC的前一天(client, admin, world, east_eight):
    assert "2026-10-21T07:30" not in {s for _, s, _ in _edu(client, admin, world["patients"][0], "2026-10-20")}
    assert "2026-10-21T07:30" in {s for _, s, _ in _edu(client, admin, world["patients"][0], "2026-10-21")}


def test_只带这位患者的(client, admin, world):
    assert _edu(client, admin, world["patients"][1], "2026-10-20") == [(world["title"], "2026-10-20 09:00:00", "pending")]


def test_页面日历画出宣教一段():
    start = PAGE.index('$("#spd-cal-form").onsubmit')
    handler = PAGE[start:PAGE.index("\n  };", start)]
    assert "宣教 ${(cal.edu || []).length}" in handler
    assert "cal.edu || []" in handler and "spdTag(SPD_PUSH_STATUS, d.status)" in handler
