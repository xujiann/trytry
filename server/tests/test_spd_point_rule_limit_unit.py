"""村医积分规则的「每日上限」写明是分值、不是次数（P2-604，第十二批「前端提示 vs 后端校验」扫描 Z3-6）。

`service.award_points` 按「当天该规则已入账分值 + 本次积分 > 上限」封顶（docstring：`daily_limit` 单位是分），页面上
新建的占位、清单的表头、编辑框的标签都只写「每日上限」——按次数理解的人把「每天最多 3 次、每次 10 分」配成上限 3，
这条规则从此一分都记不上。按分值封顶的行为本身由 `tests/test_spd_points_daily_limit_local_day.py` 等盯着，这里只管页面写明。
"""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


def test_页面三处都写明按分值封顶():
    start = PAGE.index('${panel("村医积分规则"')
    body = PAGE[start:PAGE.index('${panel("积分商品与核销"', start)]
    assert '<b>分值</b>封顶、不是次数' in body
    assert 'placeholder="每日上限(分)"' in body and '"每日上限(分)"' in body
    assert 'label: "每日上限（分，按当天已入账分值封顶、不是次数；0 = 不限）"' in PAGE
