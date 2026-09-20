"""慢专病考核与积分页的界面入口守卫（P1-40，spd.assess 12 → 0）。

全平台棘轮只回答"这条路径有没有人调用过"。这一块还有几件它看不见的事：

* **改指标前要能看见谁在引用它**：指标的权重/公式一改，所有引用它的方案得分
  口径跟着变——包括已经跑过的历史分。`/indicators/{id}/usage` 存在的全部意义
  就是让人在改之前看见这件事，把它做成一个孤零零的查询没有价值。
* **得分分析必须指定方案**：`scores-analysis` 的 `plan_id` 是必填，前端不挡就是
  拿一个 422 当提示。
* **积分规则改了不回算历史**：积分是发出去的，回算等于事后改账。界面上要把这
  句话说出来，否则管理员会以为调低分值能把已发的分收回来。
"""
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
SPD_JS = (STATIC / "pages-spd.js").read_text(encoding="utf-8")


def _block() -> str:
    start = SPD_JS.index("async function renderSpdAssess()")
    end = SPD_JS.index("async function renderSpdFollowupCenter()", start) \
        if "async function renderSpdFollowupCenter()" in SPD_JS \
        else len(SPD_JS)
    return SPD_JS[start:end]


BLOCK = _block()


@pytest.mark.parametrize(
    "path",
    [
        "/api/spd/indicators/${",
        "/usage`",
        "/api/spd/assess-plans/${",
        "/api/spd/point-rules",
        "/api/spd/point-rules/${",
        "/api/spd/point-accounts/signin",
        "/api/spd/redeems",
        "/api/spd/goods/${",
        "/api/spd/scores-analysis",
        "/api/spd/workload",
    ],
)
def test_考核各端点都在本页调到(path):
    assert path in BLOCK, f"本页没有调用 {path}"


def test_改指标前能看见引用它的方案():
    """权重/公式一改，引用它的方案得分口径跟着变——包括已经跑过的历史分。"""
    assert "data-ind-use" in BLOCK, "指标行没有「看引用」入口"
    idx = BLOCK.index("indUse.dataset.indUse}/usage")
    around = BLOCK[idx:idx + 600]
    assert "历史与后续得分口径都会变" in around, (
        "看引用只列了方案，没说清「改它会让这些方案的得分口径变」——"
        "那正是这个查询存在的意义"
    )


def test_得分分析必须指定方案():
    assert "得分分析要指定方案ID" in BLOCK, (
        "scores-analysis 的 plan_id 是必填，前端不挡就是拿一个 422 当提示"
    )


def test_积分规则说明不回算历史():
    assert "不会回算历史积分" in BLOCK, (
        "没说清规则改了只影响之后的事件——积分是发出去的，"
        "管理员会以为调低分值能把已发的分收回来"
    )


def test_无库存商品不给兑换按钮():
    """后端库存不足会拒；给了按钮就是给一个必然报错的入口。"""
    idx = BLOCK.index('data-goods-redeem="')
    around = BLOCK[max(0, idx - 200):idx]
    assert "g.stock > 0" in around, "无库存的商品仍给了兑换按钮"


def test_兑换有二次确认():
    assert "兑换后会生成核销码" in BLOCK, "兑换没有二次确认，误点就扣积分"


def test_日上限0显示为不限():
    """0 在这里是"不限"不是"一分都不给"，直接显示 0 会被读反。"""
    assert 'r.daily_limit || "不限"' in BLOCK, (
        "日上限 0 直接显示成 0——它的语义是「不限」，显示 0 会被读成「一分都不给」"
    )
