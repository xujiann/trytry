"""原生表单提交竞态的双层守卫（CI 实锤，2026-08-31；P2-31 逐页根修后推广为清单式）。

现场：管理端「启动路径」表单在慢 runner 上打出
``/?enrollment_id=1&template_id=1#spdpath``——渲染器"innerHTML 画表单 →
await 取数 → 挂监听"的两趟网络往返里点了提交，submit 没人接管，浏览器走
原生 GET：表单数据泄进 URL、整页重载、POST 根本没发出。本地机器窗口只有
几毫秒从未现形，CI 两次失败同一 URL 形状。

两层修法各钉其一：
1. **根修**：渲染器把监听挂在与首屏 innerHTML 同一个同步块（任何 await 之前），
   取数放最后，窗口为零。最初只有 renderSpdPath 一处，P2-31 已逐页推广——
   清单见下方 ``ROOT_FIXED_RENDERERS``，每个已根修的渲染器钉一条锚点断言。
2. **类兜底**：shared.js 在 document 层 preventDefault 所有漏网的 submit
   （全仓库没有一个 <form action=…>，原生提交从来不是意图）——把"导航+
   丢数据"降级成"这一下没生效"。

**登记在案的例外**（监听回调闭包依赖 await 取回的数据，提前挂会把窗口期
提交从「兜底无效」变成「TypeError/空提交」，非零行为差，故保持晚挂、由
document 层兜底护住；各处代码内有同文注释）：
- pages-spd.js renderSpdAdmin  #spd-program-form（依赖 spdMeta→规则编辑器）
- pages-spd.js renderSpdPatients  #spd-group-form（同上，其余 6 处已根修入清单）
- pages-spd.js renderSpdReferral  #spd-refrule-form（同上；其余监听本就在同步块）
- pages-spd.js renderSpdFollowup  #spd-quest-form（同上，其余 5 处已根修入清单）
- m/m.js renderSpdScreen（提交回调读取 await 之后才画出的 [data-q] 选项）

**检查机理**：把函数体从 ``async function 名(`` 粗切到下一个顶层
``function``/``async function`` 声明；断言清单里每条监听挂载锚点的出现位置
都在「末尾取数锚点」之前。锚点要求在函数体内**恰好出现一次**——歧义直接红，
避免"锚到 handler 内部的同名调用"造成假绿。少数取数语句与 handler 内部调用
同文（如 ``await draw();``），锚点带上换行+两空格缩进前缀（``\\n  await …``）
只匹配函数顶层那一条。

**局限**：纯词法检查，不解析作用域——它分不清 await 在函数自身流程里还是在
嵌套 handler 里，所以钉的是"这条具体的取数语句在监听之后"而非"监听前无任何
await"；渲染器改结构（换 helper 名、拆函数）会以「锚点丢失/歧义」的方式红，
此时应同步更新清单条目，而不是删掉它。清单只收已根修的渲染器；新增渲染器
照 renderSpdPath 样板写完后**应当**顺手加进清单。
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def test_shared层有原生提交兜底():
    src = (STATIC / "shared.js").read_text(encoding="utf-8")
    assert re.search(
        r'document\.addEventListener\("submit",\s*\(e\)\s*=>\s*e\.preventDefault\(\)\)', src
    ), "shared.js 丢了 document 层 submit 兜底——渲染窗口期的提交会退回原生 GET 导航"


def test_三个入口都先加载shared():
    """兜底只有先加载才有效（这也是 $/esc 的既有约定，这里对本守卫再钉一次）。"""
    for entry in ("index.html", "m/index.html", "m/doctor.html"):
        html = (STATIC / entry).read_text(encoding="utf-8")
        scripts = re.findall(r'<script src="([^"]+)"', html)
        assert scripts and scripts[0].endswith("shared.js"), (
            f"{entry} 的第一个 script 不是 shared.js：{scripts[:2]}"
        )


# ---- 清单式根修守卫 -------------------------------------------------------
# (文件, 渲染函数名, [监听挂载锚点…], 末尾取数锚点)
# 锚点是函数体内唯一的一段源码；监听锚点必须全部出现在取数锚点之前。
ROOT_FIXED_RENDERERS = [
    ("pages-spd.js", "renderSpdPath",
     ['$("#spd-tpl-form").onsubmit', '$("#spd-inst-form").onsubmit',
      '$("#spd-task-filter").onsubmit', '$("#page-body").onclick'],
     "await Promise.all([drawInstances(), drawTasks()])"),
    ("core.js", "renderPatients",
     ['$("#auth-grant-form").onsubmit', '$("#auth-list-form").onsubmit',
      '$("#auth-check-form").onsubmit', '$("#page-body").onclick',
      '$("#patient-form").onsubmit', '$("#patient-search").onsubmit'],
     "\n  await draw();"),
    ("core.js", "renderDicts",
     ['$("#dict-system").onchange', '$("#dict-form").onsubmit'],
     'await draw("diagnosis");'),
    ("pages-public.js", "renderCerts",
     ['$("#cert-form").onsubmit', '$("#cert-filter").onsubmit',
      '$("#chk-form").onsubmit', '$("#page-body").onclick'],
     "\n  await draw();"),
    ("pages-public.js", "renderEsb",
     ['$("#esb-msg-filter").onsubmit', '$("#esb-ep-form").onsubmit',
      '$("#esb-flow-form").onsubmit', '$("#page-body").onclick'],
     "\n  await drawMessages();"),
    ("pages-public.js", "renderDataQuality",
     ['$("#qc-run-form").onsubmit', '$("#page-body").onclick'],
     "await drawViolations();"),
    ("pages-public.js", "renderKnowledge",
     ["kb.onsubmit", '$("#kb-search").onsubmit', '$("#page-body").onclick'],
     "\n  await draw();"),
    ("pages-clinical.js", "renderAudit",
     ['$("#audit-search").onsubmit'],
     "\n  await draw();"),
    ("pages-clinical.js", "renderAccessLogs",
     ['$("#al-search").onsubmit'],
     "\n  await draw();"),
    ("pages-clinical.js", "renderConsents",
     ['$("#ct-search").onsubmit', '$("#cr-table").onclick'],
     "await drawConsents(); await drawCorrections();"),
    ("pages-spd.js", "renderSpdPatients",
     ['$("#spd-screen-form").onsubmit', '$("#spd-autoscreen-form").onsubmit',
      '$("#spd-enroll-form").onsubmit', '$("#spd-enroll-filter").onsubmit',
      '$("#spd-life-form").onsubmit', '$("#page-body").onclick'],
     "await Promise.all([drawScreenings(), drawEnrollments(), drawLifecycle()]);"),
    ("pages-spd.js", "renderSpdFollowup",
     ['$("#spd-fuplan-form").onsubmit', '$("#spd-fumatch-form").onsubmit',
      '$("#spd-fu-filter").onsubmit', '$("#spd-qc-form").onsubmit',
      '$("#page-body").onclick'],
     "await drawRecords();"),
]


def _render_fn_body(filename: str, fn_name: str) -> str:
    """函数体粗切：从声明行起，到下一个顶层 function 声明为止（够用且不配平花括号）。"""
    src = (STATIC / filename).read_text(encoding="utf-8")
    m = re.search(rf"async function {re.escape(fn_name)}\(", src)
    assert m, f"{filename} 里找不到 async function {fn_name}——渲染器没了？请同步更新本守卫清单"
    nxt = re.search(r"\n(?:async )?function\s", src[m.end():])
    return src[m.start(): m.end() + nxt.start()] if nxt else src[m.start():]


@pytest.mark.parametrize(
    "filename,fn_name,listen_anchors,fetch_anchor",
    ROOT_FIXED_RENDERERS,
    ids=[f"{f}:{fn}" for f, fn, _, _ in ROOT_FIXED_RENDERERS],
)
def test_已根修渲染器在await取数之前挂监听(filename, fn_name, listen_anchors, fetch_anchor):
    """根修不许回退：清单内渲染器的全部监听挂载必须先于末尾那次取数。"""
    body = _render_fn_body(filename, fn_name)
    assert body.count(fetch_anchor) == 1, (
        f"{filename} {fn_name}：取数锚点出现 {body.count(fetch_anchor)} 次（应恰好 1 次）——"
        f"渲染器结构变了，请同步更新本守卫的清单条目，别让锚点歧义变成假绿"
    )
    fetch_pos = body.find(fetch_anchor)
    for anchor in listen_anchors:
        assert body.count(anchor) == 1, (
            f"{filename} {fn_name}：监听锚点 {anchor!r} 出现 {body.count(anchor)} 次（应恰好 1 次）——"
            f"渲染器结构变了，请同步更新本守卫的清单条目"
        )
        assert body.find(anchor) < fetch_pos, (
            f"{filename} {fn_name}：{anchor!r} 挂到了 await 取数之后——网络往返窗口里点提交会"
            f"走原生 GET 导航（表单数据进 URL、POST 不发出，CI 抓过两次）；"
            f"监听必须与 innerHTML 同一同步块，取数放最后（样板见 pages-spd.js renderSpdPath）"
        )
