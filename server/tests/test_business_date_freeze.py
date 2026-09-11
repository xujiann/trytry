"""冻住业务日期：把"判据与被判对象取自两个时刻"那条窗口真正归零。

## 这条线是怎么走到这里的

1. CI run 552 红了 12 条，全是差一天——单元档跑了 62 分钟、跨过午夜，
   而七个测试文件在**模块顶层**快照了 `TODAY`。
2. 第一步把快照改成**用例里现取**，窗口从一小时缩到毫秒。
   当时就写明了：**窗口没有归零**，服务端仍然在请求发生时才取日期。
3. 要归零就得让时间可冻，而可冻的前提是**只有一个取日期的地方**——
   那时 `app/` 里还有 69 处 `date.today()` 各自问系统时钟。
4. P1-53 把这 69 处收敛到 `clock.today()`，并禁掉 `from …clock import today`
   这种会逃过冻结的写法。本文件验证第 4 步确实把第 3 步的前提办成了。

## 为什么"冻住了大部分"比没冻更糟

只要有一个模块是 `from ..clock import today`（收敛前 `surgery.py` 正是如此），
换掉 `app.clock.today` 对它无效——你会**以为**时间被冻住了，
于是把偶发的差一天当成 flake 去重跑。假的确定性比没有确定性更难排查。
"""
from datetime import date, timedelta

import pytest

from conftest import business_today, freeze_business_date

FROZEN = date(2026, 3, 1)


@pytest.fixture(scope="module")
def admin_headers(client):
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_冻住之后服务端算出的业务日期就是冻住的那天(client, admin_headers):
    """端到端：服务端不再去问系统时钟。

    `/api/vaccine-supply/expiring` 的 `today` 由 `deps.resolve_business_date(None)`
    给出，正是 P1-53 收敛掉的那一族里最常用的入口。
    """
    live = client.get("/api/vaccine-supply/expiring?days=30", headers=admin_headers)
    assert live.status_code == 200, live.text
    assert live.json()["today"] == business_today().isoformat()

    with freeze_business_date(FROZEN):
        frozen = client.get("/api/vaccine-supply/expiring?days=30", headers=admin_headers)
        assert frozen.status_code == 200, frozen.text
        assert frozen.json()["today"] == "2026-03-01"


def test_冻结会自动解除(client, admin_headers):
    """离开 with 之后必须回到真实时钟——否则一个用例能把后面整档都带偏。"""
    with freeze_business_date(FROZEN):
        pass
    after = client.get("/api/vaccine-supply/expiring?days=30", headers=admin_headers)
    assert after.json()["today"] == business_today().isoformat()


def test_异常路径也会解除冻结(client, admin_headers):
    """用例断言失败时也要还原，否则失败会传染给后面的用例。"""
    with pytest.raises(RuntimeError):
        with freeze_business_date(FROZEN):
            raise RuntimeError("用例中途炸了")
    assert client.get("/api/vaccine-supply/expiring?days=30",
                      headers=admin_headers).json()["today"] == business_today().isoformat()


def test_跨午夜不再影响判据(client, admin_headers):
    """本条直接回应 run 552：冻住之后，**真实时钟怎么走都不影响判据**。

    以前要靠"发请求与断言之间别跨午夜"；现在判据与被判对象取自同一个冻住的值，
    两次请求之间即便真的跨了午夜，结果也一样。
    """
    with freeze_business_date(FROZEN):
        first = client.get("/api/vaccine-supply/expiring?days=30",
                           headers=admin_headers).json()["today"]
        second = client.get("/api/vaccine-supply/expiring?days=30",
                            headers=admin_headers).json()["today"]
        assert first == second == FROZEN.isoformat()


def test_只冻日期不冻时间戳(client, admin_headers):
    """说清楚它不保证什么：落库时间戳走 `models.utcnow()`，不在冻结范围内。

    两者是平台里两把不同的尺子。把时间戳也一起冻掉，会让「对账日切在 UTC」
    那类问题在测试里消失、而线上还在（P2-35）。
    """
    from app.models import utcnow

    with freeze_business_date(date(2000, 1, 1)):
        assert utcnow().year != 2000, "时间戳不该被业务日期的冻结带跑"


def test_冻结覆盖派生函数(client, admin_headers):
    """`clock.today_str()` 内部调 `today()`，走的是模块全局查找，必须一起被冻住。

    如果哪天有人把 `today_str` 改成自己去问 `date.today()`，这条会红。
    """
    from app import clock

    with freeze_business_date(FROZEN):
        assert clock.today() == FROZEN
        assert clock.today_str() == "2026-03-01"
        assert clock.today() + timedelta(days=1) == date(2026, 3, 2)
