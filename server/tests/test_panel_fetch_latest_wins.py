"""面板取数不比对请求序号：先发的旧响应晚到，就盖掉后选的那一份（P2-1012，第二十九批「页面状态残留」扫描 E2-5）。

住院管理「医嘱单」：先点甲、立刻改点乙，甲那次响应晚到（医嘱多、响应慢）把甲的医嘱画在面板里，标题只写「医嘱单」——
真浏览器实测「登记执行」记到了甲的医嘱上。同形：编码字典连着切、居民端号源连着改机构 / 日期、价格公示展开时的全表请求
常比按关键字的那次晚回来。`route()`（core.js）、`loadSpd()`（m.js）早已用序号修过同一个毛病。

修法：取数前记下序号，回来时序号过期就丢弃；医嘱单标题写上住院号。端到端用例在 `tests/e2e/test_flows.py`。
"""
from pathlib import Path

from jssrc import strip_comments

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _src(name):
    return strip_comments((STATIC / name).read_text(encoding="utf-8"))


def _after(src, marker, length=900):
    start = src.index(marker)
    return src[start:start + length]


def _guarded(body, counter, call):
    bump = f"const seq = ++{counter};"
    check = f"seq !== {counter}"
    assert bump in body, f"没有取序号：{counter}"
    assert body.index(bump) < body.index(call) < body.index(check), f"回来时没比对序号：{counter}"


def test_医嘱单只画最后点的那一次住院_标题写住院号():
    src = _src("pages-clinical.js")
    assert "let ordersSeq = 0;" in src
    body = _after(src, "if (d.orders) {")
    _guarded(body, "ordersSeq", "await api(`/api/inpatient/orders?admission_id=")
    assert '$("#inp-orders-title").textContent = `医嘱单 · 住院 #${d.orders}`;' in body
    assert 'id="inp-orders-title"' in src


def test_编码字典只画最后切的那一个():
    body = _after(_src("core.js"), "const draw = async (system) => {")
    _guarded(body, "dictSeq", "await api(`/api/dictionaries/")


def test_居民端号源与价格公示只画最后一次查的():
    src = _src("m/m.js")
    _guarded(_after(src, "const drawSlots = async () => {"), "slotSeq", "await authApi(`/api/portal/me/slots")
    _guarded(_after(src, "async function loadPriceList("), "priceSeq", "await api(`/api/portal/price-list")
