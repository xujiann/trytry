"""日期查询参数只有一个校验真源：`datetypes.check_date`（P1-58）。

body 字段走 `DateStr` / `OptionalDateStr`，查询参数走 `deps.require_date`——两者都落到
`check_date`。本文件守查询参数这一半，并记录一处**校验随解释器升级悄悄变松**的实例。

## `resolve_business_date` 曾是第三套日期校验

41 个查询参数（38 个 `today`，外加 `from_date` ×2、`until`、`start`、`end`）经它校验，
它自己用 `date.fromisoformat`。这个函数从 Python 3.11 起放宽成接受 ISO 8601 的各种变体：
`20260901`、`2026-W36-2`、`2026W362` 都照过，而接口规范（docs/接口对接规范.md）写的是
`YYYY-MM-DD`。**没有任何一行代码改动，校验就变松了。**

后果不止是口径不一。`analytics.patient_flow` 拿解析后的日期筛县内就诊、拿**原串**筛
县外就诊；`?start=20260901` 时县外那一侧按字符串比较（`"2026-09-10" < "20260901"`）
整段落空，县外就诊从 1 变 0，县域就诊率虚高——修复前实测如此。

另一处小毛病：借它校验的 `from_date` / `until` / `start` / `end` 报错一律写着
"today 参数须为 YYYY-MM-DD 格式"，调用方看不出错的是哪个参数。
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi import HTTPException

from app import clock, deps

#: Python 3.11+ 的 `date.fromisoformat` 接受、而接口规范不接受的写法。
ISO_VARIANTS = ["20260901", "2026-W36-2", "2026W362"]


# ---------------------------------------------------------------- 一、resolve_business_date


@pytest.mark.parametrize("value", ISO_VARIANTS)
def test_resolve_business_date不再接受ISO变体(value):
    assert date.fromisoformat(value) == date(2026, 9, 1), "前提：解释器确实接受这些写法"
    with pytest.raises(HTTPException) as exc:
        deps.resolve_business_date(value)
    assert exc.value.status_code == 422
    assert exc.value.detail == "today 参数须为 YYYY-MM-DD 格式", "today 的文案一字未改"


@pytest.mark.parametrize("value", ["", "abc", "2026-02-31", "2026-9-1", "2026-09-01\n"])
def test_resolve_business_date原本就拒的照样拒(value):
    with pytest.raises(HTTPException) as exc:
        deps.resolve_business_date(value)
    assert exc.value.status_code == 422


def test_resolve_business_date合法值与缺省_行为未变():
    assert deps.resolve_business_date("2026-09-01") == date(2026, 9, 1)
    assert deps.resolve_business_date(None) == clock.today()


def test_resolve_business_date报错写出真实参数名():
    with pytest.raises(HTTPException) as exc:
        deps.resolve_business_date("abc", field="from_date")
    assert exc.value.detail == "from_date 参数须为 YYYY-MM-DD 格式"


# ---------------------------------------------------------------- 二、借它校验的五个调用方


@pytest.mark.parametrize(
    "path, param",
    [
        ("/api/appointments/doctors", "from_date"),
        ("/api/resources/match/slots", "from_date"),
        ("/api/analytics/patient-flow", "start"),
        ("/api/analytics/patient-flow", "end"),
        ("/api/audit/export", "until"),
    ],
)
def test_五个非today调用方_报错写对参数名且拒ISO变体(client, admin, path, param):
    for bad in ["abc", *ISO_VARIANTS]:
        resp = client.get(path, params={param: bad}, headers=admin)
        assert resp.status_code == 422, (bad, resp.text)
        assert resp.json() == {"detail": f"{param} 参数须为 YYYY-MM-DD 格式"}, bad


def test_县域就诊率的分子分母用同一个日期窗口(client, admin):
    """修复前：`?start=20260901` 时县内按解析后的日期筛、县外按原串筛，
    县外就诊整段落空（实测 1 → 0）。现在这种写法在入口就被拒，
    合法写法下县外就诊照常计入。"""
    patient = client.post(
        "/api/patients",
        json={"name": "流向口径", "id_card": "330782198701017777"},
        headers=admin,
    ).json()
    created = client.post(
        "/api/analytics/outbound-visits",
        json={"patient_id": patient["id"], "visit_date": "2026-09-10",
              "external_org_name": "市一院"},
        headers=admin,
    )
    assert created.status_code == 201, created.text

    base = client.get(
        "/api/analytics/patient-flow", params={"start": "2026-09-01"}, headers=admin
    ).json()["outside_visits"]
    assert base >= 1
    assert client.get(
        "/api/analytics/patient-flow", params={"start": "20260901"}, headers=admin
    ).status_code == 422
