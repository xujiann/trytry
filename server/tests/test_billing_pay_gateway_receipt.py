"""统一支付页的渠道下拉没有「网关支付」、网关受理（pending）的回执报「支付失败」（P2-1021，第二十九批「前后端取值表」扫描 E1-1）。

后端 `billing.PAYMENT_CHANNELS` 有五种渠道（`gateway` 是 I2 的 HTTP 支付网关，异步语义：下单只受理，单子停在 pending 等网关
回调转已支付），页面的 `PAY_CHANNELS` 只有四种——配好了网关，页面上也选不到，只能直接调接口。回执又只分 paid / 其余两种：
网关受理成功也写「支付失败：」后面跟个空原因，而付款链接 / 二维码串只在这张下单回执里带回来，一并丢掉。

修法：页面渠道表与后端同一张；回执分三态，pending 写「已受理，待网关回调确认到账」并带出二维码串与付款链接（只认 http(s)）。
页面怎么画由 e2e `test_统一支付选得到网关支付_受理回执写待回调_不报支付失败` 盯着。
"""
import re
from pathlib import Path

from app.routers import billing

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")


def _js_table(name):
    match = re.search(rf"^const {name} = \{{(.*?)\}};", PAGE, re.M)
    assert match, name
    return dict(re.findall(r'(\w+): "([^"]*)"', match.group(1)))


def test_页面渠道表与后端同一张():
    assert _js_table("PAY_CHANNELS") == billing.PAYMENT_CHANNELS   # 修前缺 gateway


def test_下单入参放行的渠道就是渠道表那几种():
    pattern = billing.PaymentCreate.model_json_schema()["properties"]["channel"]["pattern"]
    assert set(re.fullmatch(r"\^\((.*)\)\$", pattern).group(1).split("|")) == set(billing.PAYMENT_CHANNELS)


def test_回执分三态_受理写待回调_链接只认http():
    start = PAGE.index('$("#pay-form").onsubmit = async (e) => {')
    body = PAGE[start:PAGE.index("const payChannel =", start)]
    pending = body.index('if (order.status === "pending") {')
    failed = body.index("`支付失败：${order.fail_reason}`")
    assert pending < failed   # 修前没有 pending 这一支，受理成功落进「支付失败」
    assert "已受理，待网关回调确认到账" in body and "order.qr_code" in body
    # 判据收进了 core.js 的 isHttpUrl（P2-1429：课件外链、直播回放与这里共用一处），只换调用、不改判据
    assert "if (isHttpUrl(order.pay_url)) {" in body
    core = (Path(__file__).resolve().parents[1] / "app" / "static" / "core.js").read_text(encoding="utf-8")
    helper = core[core.index("function isHttpUrl(url) {"):]
    assert helper[:helper.index("\n}\n")].endswith('return /^https?:\\/\\//i.test(url || "");')
    assert "innerHTML" not in body   # 网关应答里的串一律按文本 / DOM 节点放，不拼 HTML
