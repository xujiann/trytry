"""慢专病配置域（量表/宣教/服务包/标签）的界面入口守卫。

`tests/test_frontend_endpoint_coverage.py` 那道全平台棘轮只回答"这条路径有没有
人调用过"；本文件守的是它**看不见**的那几件事——和
`test_frontend_call_site_coverage.py` 对四个模块的分工是同一个形状：

* **量表的三态操作要按状态给**：未发布的给「发布」，已发布的才给「二维码」与
  「停用」。全平台棘轮只要看到路径被调用过就算命中，看不出按钮给错了行——
  而未发布的量表取二维码后端是 409，给了按钮等于给一个必然报错的入口。
* **二维码照抄村医绑定码的既有做法**（`<img src>` 直挂鉴权 URL），不另造取文本
  的助手。写这页时我一度凭空写了个 `apiText()` ——仓库里根本没有这个函数。
* **JSON 入参要在前端挡一道**：题目/计分/包内项目都是 JSON 文本框，解析失败要
  当场给提示，而不是把一串非法文本发给后端换一个 422。
"""
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
SPD_JS = (STATIC / "pages-spd.js").read_text(encoding="utf-8")
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")


def _page_block() -> str:
    """截出本页函数体，避免断言被别的页面的同名片段蒙混过关。"""
    start = SPD_JS.index("async function renderSpdContentConfig()")
    end = SPD_JS.index("async function renderSpdTeamConfig()", start)
    return SPD_JS[start:end]


BLOCK = _page_block()


def test_页面已注册进导航():
    assert 'render: renderSpdContentConfig' in APP_JS, (
        "写了 renderSpdContentConfig 却没登记进 app.js 的 PAGES——页面在导航里不存在"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/spd/scales",
        "/api/spd/scales/${",
        "/publish",
        "/disable",
        "/qr.svg",
        "/api/spd/edu-materials",
        "/api/spd/service-packages",
        "/api/spd/tags",
    ],
)
def test_四类配置的端点都在本页调到(path):
    assert path in BLOCK, f"本页没有调用 {path}"


def test_量表操作按状态给而不是一股脑给():
    """未发布的量表取二维码后端是 409；给了按钮就是给一个必然报错的入口。"""
    assert 'x.status === "published"' in BLOCK, (
        "量表操作没有按状态分叉——发布/二维码/停用应当只出现在对应状态的行上"
    )
    pub_idx = BLOCK.index("data-scale-pub")
    qr_idx = BLOCK.index("data-scale-qr")
    cond_idx = BLOCK.index('x.status === "published"')
    assert cond_idx < qr_idx < pub_idx, (
        "二维码/停用应在 published 分支、发布应在 else 分支（当前顺序对不上）"
    )


def test_二维码走既有的img直挂而不是自造取文本助手():
    """仓库里没有 apiText()：写这页时我一度凭空用了它，靠这条钉住。"""
    assert "apiText(" not in BLOCK, "用了仓库里不存在的 apiText()"
    assert '<img src="/api/spd/scales/' in BLOCK, "二维码没照抄村医绑定码的 <img src> 做法"


@pytest.mark.parametrize("field", ["items", "scoring"])
def test_JSON入参在前端先解析再提交(field):
    assert f"JSON.parse(form.{field}" in BLOCK or "JSON.parse(form.items" in BLOCK, (
        f"{field} 是 JSON 文本框，必须先 JSON.parse 再提交——"
        "否则一串非法文本只能到后端换一个 422"
    )
    assert "JSON 格式有误" in BLOCK, "解析失败没有就地给提示"


def test_用户数据一律转义():
    """页面渲染的每个 ${x.<字段>} 都要经 esc()，二维码那个 <img src> 的 id 也不例外。"""
    import re

    bare = re.findall(r"\$\{x\.(\w+)\}", BLOCK)
    allowed = {"id", "period_days"}   # 纯数字字段，模板里另有 esc 的也不算违规
    bad = [f for f in bare if f not in allowed]
    assert bad == [], f"以下字段裸插值未转义：{sorted(set(bad))}"
