"""原生表单提交竞态的双层守卫（CI 实锤，2026-08-31）。

现场：管理端「启动路径」表单在慢 runner 上打出
``/?enrollment_id=1&template_id=1#spdpath``——渲染器"innerHTML 画表单 →
await 取数 → 挂监听"的两趟网络往返里点了提交，submit 没人接管，浏览器走
原生 GET：表单数据泄进 URL、整页重载、POST 根本没发出。本地机器窗口只有
几毫秒从未现形，CI 两次失败同一 URL 形状。

两层修法各钉一条：
1. **根修**：renderSpdPath 把全部监听挂在任何 await 之前（与 innerHTML 同一
   同步块，窗口为零）——钉"挂监听语句的位置在 await 取数之前"；
2. **类兜底**：shared.js 在 document 层 preventDefault 所有漏网的 submit
   （全仓库没有一个 <form action=…>，原生提交从来不是意图）——把"导航+
   丢数据"降级成"这一下没生效"。其余 90+ 页面渲染器里同样的窗口仍存在
   （已记 TECH_DEBT），兜底保证最坏情况不再是丢数据。
"""
import re
from pathlib import Path

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


def test_spdpath渲染器在await取数之前挂监听():
    """根修不许回退：`#spd-inst-form` 的监听挂载必须先于 drawInstances/drawTasks 的 await。"""
    src = (STATIC / "pages-spd.js").read_text(encoding="utf-8")
    m = re.search(r"async function renderSpdPath\(\)", src)
    assert m, "renderSpdPath 不见了？"
    # 函数体粗切到下一个顶层 async function 声明为止，够用且不依赖花括号配平
    end = src.find("\nasync function ", m.end())
    body = src[m.start(): end if end != -1 else len(src)]
    attach = body.find('$("#spd-inst-form").onsubmit')
    fetch_last = body.find("await Promise.all([drawInstances(), drawTasks()])")
    assert attach != -1 and fetch_last != -1, "锚点丢失——渲染器结构变了，请同步更新本守卫"
    assert attach < fetch_last, (
        "启动路径表单的监听又挂到了 await 取数之后——网络往返窗口里点提交会走"
        "原生 GET 导航（表单数据进 URL、POST 不发出），CI 抓过两次"
    )
