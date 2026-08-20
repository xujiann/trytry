"""居民端转诊页的静态守卫：孤岛不许重开、文案不许再分叉。

配套 ADR-0003 方案 B。接口侧把两套转诊并成了 `/api/portal/me/referrals/all`，
但只要前端还分别调两个单源接口，**用户看到的仍是两份互不相交的列表**——
接口聚合了、孤岛没消，这正是上一轮如实记下的"只做了一半"。

前端是免构建原生 JS、没有 JS 测试框架，所以这里按仓库既有的静态扫描范式
（同 `test_spd_boundary.py` 的 AST 扫描）直接对源码断言。
"""
import re
from pathlib import Path

import pytest

M_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "m" / "m.js"


@pytest.fixture(scope="module")
def source() -> str:
    return M_JS.read_text(encoding="utf-8")


def test_不再分别调两个单源转诊接口(source):
    """两个单源接口本身保留（既有契约），但居民端不该再拿它们拼列表。"""
    # 三种写法都要扫到：反引号模板串、单引号、双引号。只认反引号的话，
    # 有人用 authApi("/api/portal/spd/referrals") 就能把孤岛悄悄开回来。
    called = re.findall(
        r"""authApi\(\s*[`'"](/api/portal/(?:me|spd)/referrals[^`'"]*)""", source
    )
    offenders = [p for p in called if not p.startswith("/api/portal/me/referrals/all")]
    assert offenders == [], (
        f"居民端仍在调单源转诊接口 {offenders}——孤岛会就此重开。"
        "改用 /api/portal/me/referrals/all，慢专病页按 source 字段筛。"
    )


def test_聚合接口确实被用上了(source):
    assert "/api/portal/me/referrals/all" in source


def test_前端不再自带转诊状态文案表(source):
    """状态文案的权威在后端 `status_label`。

    理由是**同一份映射不该有两个副本**：一处改了另一处不会跟着改。这不是假想——
    业务端 `static/core.js` 的 `REF_STATUS` 至今写着"待接诊/已接诊/已结案"，
    而居民端写的是"待接收/已接收/已完成"，同一个 `pending` 在两个界面读起来就不一样。
    本轮先把居民端这一份收归后端；业务端那份另案（见 ROADMAP）。
    """
    for dead in ("REFERRAL_STATUS", "SPD_REF_TEXT"):
        assert dead not in source, f"{dead} 应已删除，改用后端 status_label"
    assert "status_label" in source, "状态文案必须取自后端 status_label"


def test_转诊卡片的值都过了esc(source):
    """`kv()` 只转义键、不转义值（见其实现），值一律要在调用点 esc()。

    CLAUDE.md §8：前端渲染用户数据一律先 esc()，innerHTML 插值不得漏转义。
    """
    match = re.search(r"function referralCard\(.*?\n\}", source, re.S)
    assert match, "找不到 referralCard，测试需要跟着改"
    card = match.group(0)
    # 逐字钉住每个**会被输出到 HTML 的**值的转义写法。
    #
    # 不用"扫描裸插值"那类通用正则：字段也会出现在纯判断位置
    # （`(r.from_org || r.to_org) ? …`、`r.direction === "up" ? "上转" : "下转"`），
    # 那些不进 HTML，通用规则要么误报、要么放水。早先写的版本就是放水的那种——
    # 正则从不命中，把 esc() 删掉测试照样绿。
    required = [
        'esc(r.from_org || "—")',
        'esc(r.to_org || "—")',
        'esc(r.reason || "—")',
        "esc(r.status_label || r.status)",
        "esc(r.date)",
        "esc(r.detail_path)",
        "esc(SOURCE_NAMES[r.source] || r.source)",
    ]
    for fragment in required:
        assert fragment in card, (
            f"卡片里应有 `{fragment}`。kv() 只转义键不转义值，漏一处就是 XSS；"
            "若确实改了写法，请把这份清单一并改掉，别直接删断言。"
        )


def test_详情链接直接用后端给的path(source):
    """后端给的 detail_path 已带 patient_id——代管家属的单子才点得开。

    前端若自己拼 `/api/portal/spd/referrals/{id}`，就会丢掉这个参数（旧代码
    是自己拼的，靠另一处补 patient_id；少一处就 404）。
    """
    assert "data-ref-detail" in source
    assert "btn.dataset.refDetail" in source
    assert not re.search(r"""[`'"]/api/portal/spd/referrals/\$?\{""", source), \
        "详情链接应直接用后端的 detail_path，不要在前端拼"


def test_慢专病页用服务端收窄而不是客户端筛(source):
    """条数上限是**合并之后**才截的，客户端 filter 会漏数据。

    居民若有 50 条更新的平台转诊，聚合结果里慢专病的单子会被整段挤出窗口，
    页面显示"暂无转诊记录"——而他其实有在办的转诊。收窄必须在服务端做。
    """
    match = re.search(r"async function renderSpdReferrals\(.*?\n\}", source, re.S)
    assert match, "找不到 renderSpdReferrals，测试需要跟着改"
    body = match.group(0)
    assert 'source: "spd"' in body, "应把 source=spd 作为查询参数交给服务端收窄"
    assert ".filter(" not in body, (
        "不要在客户端筛 source——合并后截断会把本源的单子挤掉"
    )


def test_详情按钮自带错误处理(source):
    """列表渲染之后才触发的异步不在 loadService/loadSpd 的 try 覆盖内，
    不接住的话 404/403/断网只会是一个"点了没反应"的死按钮。"""
    match = re.search(r"function bindReferralDetails\(.*?\n\}", source, re.S)
    assert match, "找不到 bindReferralDetails，测试需要跟着改"
    body = match.group(0)
    # 只 grep "catch" 是不够的：把 `try {` 删掉、留下 `} catch`，字符串仍在，
    # 但代码已经是坏的。要确认 await 真的落在 try 里——try 在它之前、catch 在它之后。
    try_at = body.find("try {")
    call_at = body.find("await authApi(")
    catch_at = body.find("catch (")   # 找真正的 catch 子句，不是注释里的"catch"二字
    assert try_at != -1, "详情按钮的异步调用必须包在 try 里"
    assert call_at != -1, "找不到 authApi 调用，测试需要跟着改"
    assert try_at < call_at < catch_at, (
        "await authApi(...) 必须落在 try…catch 之间，"
        f"实际位置 try={try_at} call={call_at} catch={catch_at}"
    )
