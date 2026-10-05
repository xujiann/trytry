"""室内质控的测定时刻比当前时刻晚一天以上的拒收（P2-1470，第四十三批扫描 AG1-10）。

修前测定时刻只查格式（`OptionalDateTimeStr`）：年份敲错的点（2026 敲成 2027）201 落库，按测定时刻永远排在 L-J 图最右；
之后每录一点都比它早，都被当成插在它前面的「补录」、去改判它——失控标记反复翻转，真的 2-2s 落在年份错的那一点上，同一张
回执先说「尚有 1 个失控点未处理」随即又把那一点改判掉。

修法：比当前时刻晚一天以上的 422「测定时刻（…）比当前时刻晚一天以上，请核对年份与日期」，补录过去的时刻照收，留空照旧按
录入时刻。容差是一天而不是几分钟：测定时刻是录入电脑的本地墙上时间（页面 datetime-local 手填，留空按 `clock.now_local()`
记，P2-171），服务器的「本地」却可以在别的时区——镜像与 compose 都不设 TZ，默认部署的容器是 UTC，东八区的钟比它快 8 小时，
按哪个时区解读是待裁定的 P1-105。容差取几分钟的话，东八区上午 10 点补录早上 8 点的点就被拒（核实时按扫描原修法实测）。
这里把进程时区拨到东八区、纽约（本地比 UTC 一早一晚）与 UTC（默认部署）三档各跑一遍。
"""
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.database import SessionLocal
from app.models import QcMeasurement


def _set_tz(name):
    if name is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = name
    time.tzset()


@pytest.fixture(params=["Asia/Shanghai", "America/New_York", "UTC"])
def local_tz(request):
    """把进程时区拨到给定时区，用完拨回去（同 test_labqc_measured_at_local.py 的拨法）。"""
    saved = os.environ.get("TZ")
    _set_tz(request.param)
    try:
        yield request.param
    finally:
        _set_tz(saved)


@pytest.fixture
def utc_server():
    """默认部署：服务器进程按 UTC 跑（镜像与 compose 都不设 TZ）。"""
    saved = os.environ.get("TZ")
    _set_tz("UTC")
    try:
        yield
    finally:
        _set_tz(saved)


@pytest.fixture(scope="module")
def lot(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21470 县检验中心", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    made = client.post("/api/labqc/lots", headers=admin, json={
        "org_id": org, "item_code": "K", "item_name": "血清钾", "lot_no": "P21470", "target_value": 5.0, "sd": 0.5})
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _count(lot_id):
    with SessionLocal() as db:
        return db.query(QcMeasurement).filter(QcMeasurement.lot_id == lot_id).count()


def _post(client, admin, lot_id, at):
    return client.post(f"/api/labqc/lots/{lot_id}/measurements", headers=admin, json={"value": 5.1, "measured_at": at})


#: （说明，相对本地当前时刻的偏移，写法，期望）——写法覆盖接口收的三种：空格、`T` 分隔、只写日期
CASES = [
    ("明年", timedelta(days=365), "%Y-%m-%d %H:%M", 422),
    ("明年_T分隔", timedelta(days=365), "%Y-%m-%dT%H:%M", 422),
    ("明年_只写日期", timedelta(days=365), "%Y-%m-%d", 422),
    ("后天", timedelta(days=2, minutes=10), "%Y-%m-%d %H:%M", 422),
    ("晚十分钟_容差内", timedelta(minutes=10), "%Y-%m-%d %H:%M", 201),
    ("晚八小时_时区之差_容差内", timedelta(hours=8), "%Y-%m-%d %H:%M", 201),
    ("补录前天", -timedelta(days=2), "%Y-%m-%d %H:%M", 201),
]


@pytest.mark.parametrize("offset, shape, expected", [case[1:] for case in CASES], ids=[case[0] for case in CASES])
def test_比本地当前时刻晚一天以上的422_容差内与补录照收(client, admin, lot, local_tz, offset, shape, expected):
    at = (datetime.now() + offset).strftime(shape)   # 进程时区已拨好：datetime.now() 就是本地墙上时间
    before = _count(lot)
    resp = _post(client, admin, lot, at)
    assert resp.status_code == expected, (local_tz, at, resp.text)   # 修前一律 201
    if expected == 422:
        assert resp.json() == {"detail": f"测定时刻（{at}）比当前时刻晚一天以上，请核对年份与日期"}
        assert _count(lot) == before   # 不落库，也就不会去改判别的点
    else:
        assert resp.json()["measured_at"] == at   # 合法值原样落库
        assert _count(lot) == before + 1


def test_默认部署按UTC跑_东八区补录今早的点照收(client, admin, lot, utc_server):
    """东八区上午 10 点补录早上 8 点测的点：录入电脑的墙上时间比 UTC 的服务器快 8 小时，这一点在服务器看来晚了 6 小时。
    容差若只有几分钟（扫描原修法），这一笔正常补录就被拒。"""
    east8_now = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)
    at = (east8_now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    resp = _post(client, admin, lot, at)
    assert resp.status_code == 201, (at, resp.text)
    assert resp.json()["measured_at"] == at


def test_留空照旧按录入时刻收(client, admin, lot):
    resp = client.post(f"/api/labqc/lots/{lot}/measurements", headers=admin, json={"value": 5.0})
    assert resp.status_code == 201, resp.text
    assert resp.json()["measured_at"]
