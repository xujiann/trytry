"""期间字符串解析：`month_bounds`（YYYY-MM）与 `period_bounds`（YYYY / YYYY-MM）。

这两个函数之前有**四份**副本（`deps.period_bounds` + `analytics._period_bounds`
+ `cost._period_bounds`，后两者逐字节相同），且都在同一个地方漏了同一个 bug：
右端点的计算写在 `try` 外面，`9999-12` 的次月是 10000 年，溢出直接 500。
合成两份（年度粒度 / 月度粒度各一）之后，这里是它们唯一的回归网。

`month_bounds` 是**特征化**用例：它服务的 5 个既有端点响应字节不许变
（CLAUDE.md §11），包括原实现宽松的地方——`2026-1` 照样接受。
"""
import pytest
from fastapi import HTTPException

from app.deps import month_bounds, period_bounds


# ------------------------------------------------------------ month_bounds
@pytest.mark.parametrize(
    "period,start,end",
    [
        ("2026-01", "2026-01-01", "2026-02-01"),
        ("2026-02", "2026-02-01", "2026-03-01"),
        ("2026-12", "2026-12-01", "2027-01-01"),   # 跨年
        ("2024-02", "2024-02-01", "2024-03-01"),   # 闰年二月
        ("2026-1", "2026-01-01", "2026-02-01"),    # 特征化：原实现不要求补零
        ("9999-11", "9999-11-01", "9999-12-01"),   # 上界内
    ],
)
def test_月度区间左闭右开(period, start, end):
    got = month_bounds(period)
    assert (got[0].isoformat(), got[1].isoformat()) == (start, end)


@pytest.mark.parametrize(
    "period",
    ["2026-13", "2026-00", "202612", "0000-01", "2026-01-01", "",
     "2026", "２０２６-０１", "2026-1a", " 2026-01"],
)
def test_月度非法输入一律422且文案不变(period):
    with pytest.raises(HTTPException) as exc:
        month_bounds(period)
    assert exc.value.status_code == 422
    # 文案是既有端点的响应体，改了就是破坏兼容
    assert exc.value.detail == "period 须为 YYYY-MM 格式"


def test_月度上溢是422不是500():
    """`9999-12` 的次月首日是 10000 年，原实现让 OverflowError 漏了出去。

    用户传一个畸形 period 得到 500，意味着这是**服务端错误**——会进告警、
    会被当成故障排查。它是入参问题，只能是 422。
    """
    with pytest.raises(HTTPException) as exc:
        month_bounds("9999-12")
    assert exc.value.status_code == 422


# ------------------------------------------------------------ period_bounds
def test_年度区间():
    assert period_bounds("2026")[1:] == (
        __import__("datetime").date(2026, 1, 1),
        __import__("datetime").date(2027, 1, 1),
    )


def test_年度粒度也认月度():
    period, start, end = period_bounds("2026-12")
    assert period == "2026-12"
    assert (start.isoformat(), end.isoformat()) == ("2026-12-01", "2027-01-01")


@pytest.mark.parametrize("period", ["9999", "0000", "9999-12", "２０２６", "abcd", "2026-13"])
def test_考核周期的越界与畸形输入一律422(period):
    """`9999-12` 这条是回归：年度形式 `9999` 当初圈进 try 了，月度形式漏在外面。"""
    with pytest.raises(HTTPException) as exc:
        period_bounds(period)
    assert exc.value.status_code == 422


def test_缺省取当年而不是全部历史():
    from app.clock import now_naive

    period, start, end = period_bounds(None)
    assert period == str(now_naive().year)
    assert (start.month, start.day) == (1, 1)
    assert end.year == start.year + 1


def test_两个解析器分工不同_月度那个不认年度():
    """让 `month_bounds` 也接受 `YYYY` 会把 5 个既有端点的 422 变成 200。

    要改先走 ADR——这条用例是防止有人"顺手统一一下"。
    """
    with pytest.raises(HTTPException):
        month_bounds("2026")
    assert period_bounds("2026")[0] == "2026"
