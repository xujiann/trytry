"""用户手册说驾驶舱有「基金」指标，驾驶舱实际没有任何基金或医保指标（P2-1513，第四十四批扫描 AH4-10）。

管理层一章的页面清单写「决策驾驶舱 | 转诊/互认/慢病/基金等核心指标与预警横幅」；驾驶舱的总览接口（`metrics.overview`）只有机构、
患者、诊疗、远程诊断、转诊、审方、慢病、缺药几块，页面（`core.js::renderDashboard`）的指标卡、趋势、慢病分级、可下钻指标目录与
绩效前 8 也没有一项基金或医保指标（扫描按代码读）。照手册去驾驶舱找基金，找不到。

修法只改手册那一行、不加驾驶舱指标：写实情——驾驶舱没有基金与医保指标，基金池、预付与结余分配在「医保基金总额付费」页，医保基金
支出监测在「医保协同」页。这里钉住：驾驶舱真的没有基金 / 医保指标时，手册那一行不把基金列进驾驶舱的指标；手册指去的两页都在页面
注册表里、页上确实有基金的内容。哪天驾驶舱真加了基金指标，前一条会红，提醒把手册一起改。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "server" / "app" / "static"
MANUAL = (ROOT / "docs" / "用户手册.md").read_text(encoding="utf-8")
FUND_WORDS = re.compile(r"基金|医保|fund|insurance", re.I)


def _function(name: str, head: str) -> str:
    source = (STATIC / name).read_text(encoding="utf-8")
    start = source.index(head)
    return source[start:source.index("\n}\n", start)]


def _dashboard_row() -> str:
    chapter = MANUAL[MANUAL.index("## 第二章 管理层（director）"):]
    chapter = chapter[:chapter.index("\n## ", 1)]
    (row,) = [line for line in chapter.splitlines() if line.startswith("| 决策驾驶舱 |")]
    return row


def test_驾驶舱没有基金与医保指标(client, admin):
    overview = client.get("/api/metrics/overview", headers=admin)
    assert overview.status_code == 200, overview.text
    assert list(overview.json()) == ["resources", "service_division", "remote_diagnosis", "referrals",
                                     "prescription_review", "chronic_management", "pharmacy"]
    assert not FUND_WORDS.search(str(overview.json()))
    assert not FUND_WORDS.findall(_function("core.js", "async function renderDashboard()"))


def test_手册的驾驶舱一行不把基金列进指标_指去基金所在的页():
    row = _dashboard_row()
    listed = row[:row.index("核心指标")]
    assert not FUND_WORDS.search(listed), row   # 修前：转诊/互认/慢病/基金等核心指标
    assert "没有基金与医保指标" in row and "「医保基金总额付费」" in row and "「医保协同」" in row, row
    # 指去的两页：注册表里就叫这个名字，页上确实有基金的内容
    registry = re.findall(r'\{ id: "[\w-]+", title: "([^"]*)", render: (\w+)', (STATIC / "app.js").read_text(encoding="utf-8"))
    render_of = dict(registry)
    assert (render_of["医保基金总额付费"], render_of["医保协同"]) == ("renderFund", "renderInsurance")
    assert 'panel("基金池"' in _function("pages-mgmt.js", "async function renderFund()")
    assert "医保基金支出总额" in _function("pages-clinical.js", "async function renderInsurance()")
