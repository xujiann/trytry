"""慢专病考核的统计周期必须先校验再展开——否则区间左右颠倒、全员 0 分且不报错。

`_period_range("2026-13")` 原先直接往下算 `int(month) % 12 + 1`，得到
`["2026-13-01", "2026-01-31"]`：左端大于右端，后面每个 `between(start, end)`
都恒空。接口返回 200、考核取数一条不剩、**全员 0 分**，没有任何错误提示。

这比"少算一个人"难查得多，与 D-3（存量假日期让派驻记录从监测指标里静默消失）
同族，而且落在**考核计分**上——直接影响机构与医师的分数。

K4 建 `PeriodStr` 时在平台侧修掉了五处同族缺陷，这一处因为没有正则字面量、
它的静态闸门认不出（已写进那道闸门的盲区声明）。本文件按行为把它钉住。
"""
import pytest
from conftest import reset_database
from fastapi.testclient import TestClient

from app.main import app
from app.spd.routers.assess import _period_range


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("period", ["2026-13", "2026-00", "2026-99"])
def test_非法月份不再算出颠倒的区间(period):
    """去掉 _period_range 里的 is_period 校验，本条必红——
    它会拿到 ('2026-13-01', '2026-01-31') 而不是 422。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _period_range(period)
    assert exc.value.status_code == 422


@pytest.mark.parametrize("period", ["2026-Q5", "2026-Q0", "2026-Qx"])
def test_非法季度同样挡住(period):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _period_range(period)
    assert "季度" in exc.value.detail


@pytest.mark.parametrize(
    "period,expect",
    [
        ("2026-08", ("2026-08-01", "2026-08-31")),
        ("2026-12", ("2026-12-01", "2026-12-31")),  # 跨年那一支
        ("2026-02", ("2026-02-01", "2026-02-28")),
        ("2026-Q3", ("2026-07-01", "2026-09-30")),
        ("2026", ("2026-01-01", "2026-12-31")),
    ],
)
def test_合法周期的区间一字不变(period, expect):
    """守卫不能误伤：三种合法形状展开出来的区间必须与修改前逐字相同。"""
    assert _period_range(period) == expect


@pytest.mark.parametrize("period", ["2026-08", "2026-Q3", "2026"])
def test_区间左端不大于右端(period):
    start, end = _period_range(period)
    assert start <= end, f"{period} 展开出颠倒的区间 [{start}, {end}]"


def test_接口层非法周期返回422而不是空统计(client, admin):
    """端到端：脏周期以前是 200 + 空统计（看着像"这个月没人"），现在是 422。"""
    bad = client.get("/api/spd/workload", params={"period": "2026-13"}, headers=admin)
    assert bad.status_code == 422, f"脏周期仍被受理：{bad.status_code} {bad.text}"

    ok = client.get("/api/spd/workload", params={"period": "2026-08"}, headers=admin)
    assert ok.status_code == 200, ok.text
