"""居民端慢专病三个新视图的入口守卫（P1-40，spd.portal 9 → 0）。

全平台棘轮只回答"这条路径有没有人调用过"。这一块还有三件它看不见的事：

* **FormData 不能带 Content-Type**：`api()` 原先写死 `application/json`，
  multipart 的 boundary 由浏览器生成并写进头里，写死之后后端会按 JSON 去解析
  一段 multipart 正文，报一个看不懂的 422。上传凭证是本轮才接上的第一个
  multipart 调用，这条约束此前没人踩过。
* **列表渲染之后才触发的异步要单独 catch**：它们不在 `loadSpd()` 的 try 覆盖
  范围内，不接住的话 404/断网只会表现成"点了没反应"的死按钮。仓库里
  `bindReferralDetails` 已经为此写过一段注释，新加的两处照同一口径。
* **代管视角要带 patient_id**：与「我的档案」的成员切换保持一致。
"""
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
M_JS = (STATIC / "m" / "m.js").read_text(encoding="utf-8")
M_HTML = (STATIC / "m" / "index.html").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "path",
    [
        "/api/portal/spd/archive",
        "/api/portal/spd/journey",
        "/api/portal/spd/assessments",
        "/api/portal/spd/edu",
        "/api/portal/spd/revisits",
        "/read`",
        "/attachments`",
    ],
)
def test_七个端点都在居民端调到(path):
    assert path in M_JS, f"居民端没有调用 {path}"


@pytest.mark.parametrize("tab", ["archive", "journey", "more"])
def test_三个页签都挂上了(tab):
    assert f'data-spd="{tab}"' in M_HTML, f"{tab} 页签不在 index.html 里，点不到"
    assert f'activeSpd === "{tab}"' in M_JS, f"{tab} 没有接进 loadSpd 的分发"


def test_FormData不带ContentType():
    """multipart 的 boundary 由浏览器生成；写死 application/json 会让后端按 JSON
    去解析一段 multipart 正文，报一个看不懂的 422。"""
    assert "options.body instanceof FormData" in M_JS, (
        "api() 没有识别 FormData——multipart 上传会被写死的 Content-Type 毁掉"
    )
    idx = M_JS.index("const isForm = options.body instanceof FormData")
    around = M_JS[idx:idx + 300]
    assert "isForm ? {} :" in around, "识别了 FormData 却仍然塞了 Content-Type"


def test_显式传的ContentType仍然说了算():
    """兼容性：调用方显式指定时不能被覆盖掉。"""
    idx = M_JS.index("const isForm = options.body instanceof FormData")
    around = M_JS[idx:idx + 300]
    assert around.index("...(options.headers || {})") > around.index("isForm ? {} :"), (
        "options.headers 必须排在后面，否则调用方显式传的 Content-Type 会被盖掉"
    )


@pytest.mark.parametrize("marker", ["data-edu-read", "data-spd-evi"])
def test_渲染后触发的异步都单独catch(marker):
    """不在 loadSpd 的 try 里，不接住就是一个「点了没反应」的死按钮。"""
    idx = M_JS.index(f'[{marker}]')
    around = M_JS[idx:idx + 900]
    assert "catch (err)" in around, f"{marker} 的异步回调没有单独 catch"


def test_上传后清空文件输入():
    """不清空的话选同一个文件第二次不会触发 change，用户以为坏了。"""
    idx = M_JS.index("data-spd-evi")
    around = M_JS[idx:idx + 1200]
    assert "inp.value = \"\"" in around, "上传后没清空 input，重选同一文件不会再触发"


@pytest.mark.parametrize("fn", ["renderSpdArchive", "renderSpdJourney", "renderSpdMore"])
def test_三个视图都带代管视角(fn):
    start = M_JS.index(f"async function {fn}(")
    end = M_JS.index("\n}", start)
    assert "spdQuery()" in M_JS[start:end], (
        f"{fn} 没走 spdQuery()——为家人查看时不会带 patient_id"
    )
