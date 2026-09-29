"""考核页「考核结果」表按方案标出来：各方案、各周期的名次原先混在一张表里，出现多个「第 1 名」，表上又没有方案列（P2-784，
第二十批「同一个数，多处口径」扫描 M1-7）。

`GET /api/spd/scores` 不给方案和周期时按名次全表排；考核页取最新 30 条混排，只有「排名 / 对象 / 周期 / 综合得分」四列。
实测：两套方案、两个周期跑分后，前 6 行依次是卫生院0 在 8 月方案 1、9 月方案 1、9 月方案 2 各一个第 1 名，接着是卫生院1
的三个第 2 名。修法：表上补「方案」列（方案名取自同页已取的方案清单，取不到的写方案编号），并写明名次是方案、周期内部排的。
默认只看最近一方案一周期、移动端「我的考核」取最近一期要比较不同写法的期别（2026-08 / 2026Q3），另是口径问题，不在此列。
"""
import os
import re

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def _assess() -> str:
    with open(os.path.join(STATIC, "pages-spd.js"), encoding="utf-8") as fh:
        source = fh.read()
    start = source.index("async function renderSpdAssess()")
    return source[start:source.find("\nasync function ", start + 1)]


def test_考核结果表有方案列_方案名取自同页的方案清单():
    body = _assess()
    panel = body[body.index('panel("考核结果"'):]
    panel = panel[:panel.index('<div id="spd-score-detail">')]
    header = re.search(r'table\(\[([^\]]+)\], scores', panel).group(1)
    assert header.startswith('"方案", "周期"'), header   # 修前没有方案列
    assert "planNames[s.plan_id]" in panel
    assert "const planNames = Object.fromEntries(plans.map((p) => [p.id, p.name]));" in body
