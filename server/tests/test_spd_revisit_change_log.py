"""复诊计划改动日志按实际改动写中文（P2-1607，第四十七批「慢专病服务域」扫描 AK2-11）。

`PATCH /api/spd/revisits/{id}` 原先没写 note 时一律追加「状态变更为{英文码}」——修前实测（scan47 ak2/r3 末行）：计划日
10-20 改到 10-27、置提醒、一次空 PATCH，三条日志都是 `状态变更为planned`：带英文状态码（P2-767 已把同类英文码从给人看的
文字里清掉），也不记改了什么，空改动也记一条。

修法：没写 note 的按这次真改了什么拼中文——「状态：已排期 → 已复诊」「计划日：原 → 新」「提醒：未提醒 → 已提醒」，
多项用「；」连；状态 / 提醒的中文与页面 `SPD_REVISIT_STATUS` / `SPD_REMIND_STATUS` 同一套字。写了 note 的照旧记 note。
一项没改（空 PATCH、只带后端不收的字段、传的值与原值相同）不追加日志。
"""
from datetime import timedelta

import pytest

from app.clock import today

B = "/api/spd"


def _day(offset: int) -> str:
    return (today() + timedelta(days=offset)).isoformat()


@pytest.fixture(scope="module")
def patient(client, admin):
    resp = client.post("/api/patients", headers=admin, json={"name": "P21607 患者", "id_card": "330106196001011607"})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


@pytest.fixture
def revisit(client, admin, patient):
    """每个用例一条新建的复诊计划（已排期、未提醒、没有日志）。"""
    resp = client.post(f"{B}/revisits", headers=admin, json={
        "patient_id": patient, "plan_date": _day(11), "dept": "全科", "items": "复查血压"})
    assert resp.status_code == 201, resp.text
    assert (resp.json()["status"], resp.json()["remind_status"], resp.json()["log"]) == ("planned", "none", [])
    return resp.json()["id"]


def _patch(client, admin, rid: int, body: dict) -> list[str]:
    resp = client.patch(f"{B}/revisits/{rid}", headers=admin, json=body)
    assert resp.status_code == 200, resp.text
    return [entry["note"] for entry in resp.json()["log"]]


def test_改计划日_日志写前后两个日期_不带英文码(client, admin, revisit):
    notes = _patch(client, admin, revisit, {"plan_date": _day(18)})
    assert notes == [f"计划日：{_day(11)} → {_day(18)}"]   # 修前「状态变更为planned」
    assert "planned" not in notes[0]


def test_置提醒_日志写提醒的中文(client, admin, revisit):
    assert _patch(client, admin, revisit, {"remind_status": "sent"}) == ["提醒：未提醒 → 已提醒"]


def test_改状态_日志写中文状态名(client, admin, revisit):
    assert _patch(client, admin, revisit, {"status": "removed"}) == ["状态：已排期 → 已移除"]
    assert _patch(client, admin, revisit, {"status": "planned"}) == ["状态：已排期 → 已移除", "状态：已移除 → 已排期"]


def test_同时改几项_一条日志逐项写(client, admin, revisit):
    notes = _patch(client, admin, revisit, {"status": "done", "actual_date": _day(0)})
    assert notes == [f"状态：已排期 → 已复诊；实际复诊日：— → {_day(0)}"]


def test_空改动不追加日志(client, admin, revisit):
    assert _patch(client, admin, revisit, {}) == []                        # 修前追加「状态变更为planned」
    assert _patch(client, admin, revisit, {"dept": "心内科"}) == []         # 后端不收的字段，等于空改动
    assert _patch(client, admin, revisit, {"plan_date": _day(11), "status": "planned", "remind_status": "none"}) == []
    assert _patch(client, admin, revisit, {"remind_status": "contacted"}) == ["提醒：未提醒 → 已联系"]   # 真改了照记


def test_写了备注的照旧记备注(client, admin, revisit):
    assert _patch(client, admin, revisit, {"remind_status": "contacted", "note": "电话/微信邀约已联系"}) == ["电话/微信邀约已联系"]
