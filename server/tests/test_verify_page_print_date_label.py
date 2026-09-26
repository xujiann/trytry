"""打印件验真页把令牌里的日期标成「打印日期」（P2-206）。

令牌签的是打印那天（`_render` 里 `issued_date=printed_at[:10]`，ADR-0015 验的是这一张打印件），验真页却标「签发日期」：
1 月 10 日签发的死亡证明 9 月 26 日补打，纸面写「签发时间：2026-01-10」，扫出来「签发日期 2026-09-26」；
同一份证明每补打一次，验真出来的「签发日期」就换一个。页面还要人「与纸面内容逐项核对」——纸面页脚印的是「打印时间」。
"""
import os

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
ROUTERS = os.path.join(os.path.dirname(__file__), "..", "app", "routers")


def test_验真页标打印日期_与纸面页脚对得上():
    with open(os.path.join(STATIC, "verify.html"), encoding="utf-8") as fh:
        page = fh.read()
    assert '<td class="k">签发日期</td>' not in page                 # 修前
    assert '<td class="k">打印日期</td><td>\' + esc(r.issued_date)' in page
    with open(os.path.join(ROUTERS, "printing.py"), encoding="utf-8") as fh:
        printing = fh.read()
    assert "issued_date=printed_at[:10]" in printing                 # 令牌里签的确是打印日期
    assert "打印时间：" in printing                                   # 纸面页脚印的是打印时间
