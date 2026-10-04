"""驾驶舱下钻：明细行只给「目标页确实列得出这一类」的指标画跳转（P2-1315，第三十八批扫描 AB1-7）。

下钻面板原先一律写「点击明细行跳转「X」业务页」，点了执行 `nav(page)`、不带任何筛选（`core.js::openDrilldown`）。按
`metrics.METRIC_QUERIES` 的 page 映射逐个看目标页实际取数：结果互认落到互认目录页，那里根本不列申请单；转诊结案 / 上转 / 下转、
退回处方、远程诊断已报告落到混排的最新 200 条，没有这一类的筛选；基层诊疗人次落到最新 50 条就诊；慢病超期的目标页也只列
最新一页（P2-1174）——超出窗口的那一行跳过去找不到。修前实测（scan38 ab1/r10）逐项打印了跳转页与目标页取数。医废滞留原先同样
列不出（滞留面板只印条数），同批 P2-1309 让它按预警接口逐包列出、就地交接之后改为可跳。

修法（最小）：只给目标页列得出的指标画行跳转（`core.js` 的 `DRILL_GO`），其余明细行照常显示、不可点，面板文案随之改。下面这张
对照表是逐个核对的结论，**写成显式表、不靠扫描推断**：目标页补了取数 / 筛选（或指标改了映射），先改表、再改 `DRILL_GO`。
"""
import re
from pathlib import Path

from app.routers.metrics import METRIC_QUERIES

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

#: 指标 → (目标页, 目标页列不列得出这一类, 依据)。列得出的，依据写目标页取这一类的那句取数（下面逐条核对它还在）
PAGE_LISTS = {
    "critical_values": ("critical", True, 'api("/api/exams/critical")'),   # 未闭环的排在最前（P1-166），上限 100 条
    "stock_alerts": ("pharmacy", True, 'api("/api/pharmacy/alerts")'),   # 与指标同一构造，库存表逐行标「缺药」
    "infectious_recent": ("infectious", True, 'api("/api/infectious/cases")'),   # 最新 500 例按报告号倒序，近 7 日发病的在最前
    "pending_reviews": ("rx", True, 'api("/api/prescriptions?status=pending_review")'),   # 待审单独取、排最前（P1-148）
    "chronic_overdue": ("chronic", False, "在管名单按分级、编号取前 500 条，超期名单只拿来数人数、给这一页打标（P2-1174）"),
    "medwaste_overdue": ("medwaste", True, 'api("/api/medwaste/alerts")'),   # 滞留面板按预警接口逐包列出、就地交接（P2-1309）
    "referrals_up": ("referrals", False, "最新 200 条 + 待接诊 / 已接诊，没有方向筛选"),
    "referrals_down": ("referrals", False, "最新 200 条 + 待接诊 / 已接诊，没有方向筛选"),
    "referrals_completed": ("referrals", False, "已结案的只在最新 200 条里，没有状态筛选"),
    "grassroots_encounters": ("archive", False, "患者 360 页只取最新 50 条就诊，含住院、不分机构层级"),
    "reported_exams": ("exams", False, "共享诊断中心只取最新 200 张，没有按已报告取"),
    "recognized_exams": ("recognition", False, "互认目录页只有目录、统计、资源，不列申请单"),
    "rejected_prescriptions": ("rx", False, "审方页单独取的只有待审，退回的只在最新 200 张里"),
}


def _source(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _drawn() -> set[str]:
    match = re.search(r"const DRILL_GO = new Set\(\[([^\]]*)\]\);", _source("core.js"))
    assert match, "core.js 里找不到 DRILL_GO（修前没有：每个指标的明细行都画跳转）"
    return set(re.findall(r'"(\w+)"', match.group(1)))


def _render_body(page: str) -> str:
    render = dict(re.findall(r'id: "([\w-]+)", title: "[^"]*", render: (\w+)', _source("app.js")))[page]
    for name in ("core.js", "pages-clinical.js", "pages-mgmt.js", "pages-public.js"):
        source = _source(name)
        start = source.find(f"async function {render}()")
        if start != -1:
            end = source.find("\nasync function ", start + 1)
            return source[start:end if end != -1 else len(source)]
    raise AssertionError(f"找不到 {render}")


def test_对照表覆盖全部下钻指标_目标页与后端映射一致():
    assert set(PAGE_LISTS) == set(METRIC_QUERIES), "新增 / 删掉了下钻指标：先逐个核对目标页能不能列出这一类，再改这张表"
    moved = {m: (METRIC_QUERIES[m]["page"], page) for m, (page, _ok, _why) in PAGE_LISTS.items()
             if METRIC_QUERIES[m]["page"] != page}
    assert moved == {}, f"指标改了跳转页，表里的结论跟着过期了：{moved}"


def test_画跳转的指标都是目标页列得出的():
    listable = {m for m, (_page, ok, _why) in PAGE_LISTS.items() if ok}
    drawn = _drawn()
    assert drawn <= listable, f"这些指标的目标页列不出这一类，明细行却画了跳转：{sorted(drawn - listable)}"
    assert listable <= drawn, f"这些指标的目标页列得出，明细行照旧该能跳：{sorted(listable - drawn)}"


def test_列得出的目标页仍按表里写的取数():
    for metric, (page, ok, anchor) in PAGE_LISTS.items():
        if ok:
            assert anchor in _render_body(page), (metric, page, anchor)


def test_面板只给能跳的指标画行跳转_文案随之改():
    source = _source("core.js")
    start = source.index("async function openDrilldown(")
    body = source[start:source.index("\nasync function ", start + 1)]
    assert "const go = DRILL_GO.has(metric);" in body
    assert '<tr${go ? ` data-drillgo="${esc(d.page)}" style="cursor:pointer"` : ""}>' in body   # 修前每行都带
    assert body.count("data-drillgo") == 1
    assert "点击明细行跳转「${esc(d.page)}」业务页" in body
    assert "业务页列不出、也筛不出这一类，明细行不跳转" in body
