"""居民端「我的住院」：点「押金余额」不再顺带请求 admissions/undefined/bill（P2-209）。

两颗按钮共用 `bill-detail` 这个样式类，费用清单的点击监听按类挂——「押金余额」按钮没有 `data-adm`，点它除了读押金，
还会请求 `/api/portal/me/admissions/undefined/bill`，拿到 422 后弹「admission_id：Input should be a valid integer」。
"""
import os

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static", "m")


def test_费用清单监听只挂在带住院号的按钮上():
    with open(os.path.join(STATIC, "m.js"), encoding="utf-8") as fh:
        source = fh.read()
    assert 'box.querySelectorAll(".bill-detail").forEach' not in source           # 修前：押金按钮也挂上了
    handler = source[source.index('box.querySelectorAll(".bill-detail[data-adm]").forEach'):]
    assert "/api/portal/me/admissions/${btn.dataset.adm}/bill" in handler[:400]
    deposit_button = source[source.index('data-dep="${a.id}"') - 40:source.index('data-dep="${a.id}"')]
    assert "data-adm" not in deposit_button                                       # 押金按钮确实不带住院号
