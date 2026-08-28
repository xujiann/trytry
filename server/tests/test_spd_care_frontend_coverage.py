"""spd/care 的每个端点都必须有前端调用点（或书面豁免）——孤儿端点棘轮。

**这条是本次补齐 31 个孤儿端点后立的桩。** 此前 care.py 的整个服务面
（监测/评估/干预/宣教/复诊/上报/健康处方/咨询）后端交付了、需求对照表也逐条
承诺了，但**没有一个界面调用它们**——咨询甚至两端都没有入口，医生工作台却在
展示一个恒为 0 的"待回复咨询"计数。孤儿端点的坏处有两面：使用者以为功能存在
（需求表写着"已实现"），攻击者拿到的却是一片没人走过的接口面。

分母**从路由对象现算**，不抄清单：care 路由每加一个端点，本用例自动把它计入，
新端点要么带着界面来、要么在 EXEMPT 里写明白为什么不需要界面。豁免只许变少。

匹配的是**源码里的调用形态**（模板字符串 `/api/spd/consults/${id}/reply`），
不是运行时行为——免构建前端没有 jest，这里守"入口存在"，行为由后端测试守。
"""
import re
from pathlib import Path

from app.spd.routers import care, portal

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
#: 三端的全部 JS：管理端（含专业侧新页）+ 居民/村医移动端
JS_FILES = sorted(STATIC.glob("*.js")) + sorted((STATIC / "m").glob("*.js"))

#: 豁免：path -> 为什么这个端点**不需要**界面。只许变少，新增须写清理由。
EXEMPT = {
    "/api/spd/measurements/batch": (
        "设备/物联网/HIS 批量回传通道（需求一#13/#16 承诺的是**接入路径**而非界面）；"
        "蓝牙血压计不点网页"
    ),
}


def _all_js() -> str:
    return "\n".join(p.read_text("utf-8") for p in JS_FILES)


def _pattern_for(path: str) -> re.Pattern:
    """把路由模板变成"前端怎么写这个调用"的正则。

    `{param}` 在免构建前端里是模板插值 `${...}` 或写死的数字；
    其余部分按字面匹配。只认路径本身，不管查询串与动词。
    """
    parts = [re.escape(seg) if not seg.startswith("{") else r"(\$\{[^}]*\}|\d+)"
             for seg in path.strip("/").split("/")]
    return re.compile("/" + "/".join(parts))


def _route_paths(router) -> list[str]:
    return sorted({route.path for route in router.routes if hasattr(route, "methods")})


def test_care每个端点都有前端调用点或书面豁免():
    js = _all_js()
    orphans = []
    for path in _route_paths(care.router):
        if path in EXEMPT:
            continue
        if not _pattern_for(path).search(js):
            orphans.append(path)
    assert not orphans, (
        "以下 care 端点没有任何前端调用点（新端点要么带界面、要么进 EXEMPT 并写明理由）：\n  "
        + "\n  ".join(orphans)
    )


def test_居民端咨询三端点都接上了():
    """患者移动端 #18：发起/列表/消息。这三条断掉，医护侧的应答界面就是空转。"""
    js = (STATIC / "m" / "m.js").read_text("utf-8")
    for path in ("/api/portal/spd/consults",):
        assert path in js, f"居民端缺 {path} 的调用"
    assert re.search(r"/api/portal/spd/consults/(\$\{[^}]*\}|\d+)/messages", js), (
        "居民端缺消息列表调用——看不到医生回复的咨询是单向喊话"
    )


def test_居民端咨询入口挂在页签上():
    """入口没挂上，函数写了也点不进去——上一轮村医绑定二维码 404 的教训：
    别只验代码存在，验**可达**。"""
    html = (STATIC / "m" / "index.html").read_text("utf-8")
    assert 'data-spd="consult"' in html, "m/index.html 缺咨询页签按钮"
    js = (STATIC / "m" / "m.js").read_text("utf-8")
    assert 'activeSpd === "consult"' in js, "loadSpd 没接 consult 分支"


def test_管理端两个新页已注册进导航():
    app_js = (STATIC / "app.js").read_text("utf-8")
    for fn in ("renderSpdMember", "renderSpdManager"):
        assert fn in app_js, f"{fn} 没注册进 PAGES——页面写了但没有入口"


def test_豁免只许变少():
    assert len(EXEMPT) <= 1, (
        f"孤儿端点豁免应保持 ≤1 项（现 {len(EXEMPT)}）；"
        "新增豁免须在 EXEMPT 里写明为什么该端点不需要界面，且总数只许变少"
    )


def test_分母确实来自路由对象():
    """反空转自检：care 路由的端点数就是当初盘点的 31。数字对不上说明
    路由结构变了（拆包/改前缀），上面的推导可能整个失效——先修推导再动豁免。"""
    assert len(_route_paths(care.router)) == 22, (
        f"care 路由现有 {len(_route_paths(care.router))} 个不同路径"
        "（当初盘点的 31 个端点是按装饰器数的，去掉同路径多动词后是 22 条路径）；"
        "若有意增删端点，同步这里的数字"
    )
