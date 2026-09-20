"""慢专病患者生命周期页的界面入口守卫（P1-40，population 16 → 4）。

`test_frontend_endpoint_coverage.py` 那道全平台棘轮只回答"这条路径有没有人调用
过"，本文件守的是它**看不见**的几件事：

* **认领按钮只给未认领的行**：后端对已认领的再认领返回 409，"不静默改人"是它
  docstring 里写死的取舍。全平台棘轮看不出按钮给错了行，而给了就是给一个必然
  报错的入口。
* **拒绝服务申请必须写理由**：理由会回到居民端。前端不挡，居民看到的就是一句
  空白的"已拒绝"。
* **改档不提交空串**：`EnrollUpdate` 的字段全是可选，空串会被当成"要改成空"——
  把团队/主管医生清掉，而用户只是没填那一栏。
"""
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
SPD_JS = (STATIC / "pages-spd.js").read_text(encoding="utf-8")


def _block() -> str:
    start = SPD_JS.index("async function renderSpdPatients()")
    end = SPD_JS.index("async function renderSpdPath()", start)
    return SPD_JS[start:end]


BLOCK = _block()


@pytest.mark.parametrize(
    "path",
    [
        "/api/spd/candidates/${",
        "/claim",
        "/status",
        "/api/spd/enrollments/${",
        "/api/spd/patients/${",
        "/api/spd/recalls",
        "/progress",
        "/api/spd/service-applies",
        "/handle",
        "/members",
    ],
)
def test_生命周期各端点都在本页调到(path):
    assert path in BLOCK, f"本页没有调用 {path}"


def test_认领按钮只给未认领的行():
    """后端对已认领的再认领是 409；给了按钮就是给一个必然报错的入口。

    **这条断言的第一版是空洞的**：它查 `"c.claimed_team_id ?"` 是否出现，而该
    字符串在表格的「认领团队」列里本来就有一份——把按钮改成无条件渲染，它照样
    绿。改成盯 `data-cand-claim` 那一处**紧邻的**条件分叉才真的红。
    """
    # 这条断言返工了三次，每一次都是"看着在守、其实空转"的一种：
    #   ① 查 `"c.claimed_team_id ?" in BLOCK` —— 表格的「认领团队」列里本来就有
    #      一份，把按钮改成无条件渲染照样绿；
    #   ② 改成锚点定位，但锚点取了 `data-cand-claim` 的**第一次**出现——那是事件
    #      派发器里的 `el("data-cand-claim")`，围着它找条件永远找不到；
    #   ③ 锚点对了，但判据仍是宽松前缀 `claimed_team_id ?`，200 字符窗口回溯时
    #      够到了同一行的 `c.claimed_team_id ?? "—"`（`??` 也以 `?` 开头），又绿。
    # 现在盯的是那个三元**特有的空串分支**，别的写法碰不到它。
    idx = BLOCK.index('data-cand-claim="')
    around = BLOCK[max(0, idx - 200):idx]
    assert 'claimed_team_id ? ""' in around, (
        "认领按钮没有按 claimed_team_id 分叉——已被他人认领的行不该再出现「认领」"
        "（后端会 409，等于给一个必然报错的入口）"
    )


def test_拒绝服务申请必须写理由():
    assert "拒绝必须写明理由" in BLOCK, (
        "拒绝没有强制理由——理由会回到居民端，空白的「已拒绝」等于没给说法"
    )


def test_改档不提交空串():
    """EnrollUpdate 字段全可选，空串会被当成「改成空」，把团队/主管医生清掉。"""
    assert 'filter(([, v]) => v !== "")' in BLOCK, (
        "改档没有过滤空串——用户只是没填那一栏，不是要把它清空"
    )


def test_移出分组有二次确认():
    assert "手工加入的成员移出后不会被规则再吸回来" in BLOCK, (
        "移出分组没有说清后果：手工成员移出后自动规则不会把他吸回来"
    )


def test_明细面板不越点越长():
    """每次点「明细」都 insertAdjacentHTML，不清理就会越堆越多。"""
    assert "#spd-enr-detail" in BLOCK and "dup[0].remove()" in BLOCK, (
        "明细是插入式渲染，必须清掉上一份，否则连点几次页面会越来越长"
    )


def test_状态词表能喂给下拉():
    """`spdOptions` 既吃扁平词表也吃带配色的 ['文案','tag类名']——
    不分辨的话下拉里会出现「疑似,orange」这种东西。"""
    assert "Array.isArray(label) ? label[0] : label" in SPD_JS, (
        "spdOptions 没处理带配色的词表形状"
    )
