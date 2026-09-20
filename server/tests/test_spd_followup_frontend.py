"""随访中心页的界面入口守卫（P1-40，spd.followup 9 → 0）。

全平台棘轮只回答"这条路径有没有人调用过"。这一块还有几件它看不见的事：

* **抽查只写不读**：页面原先只有「生成抽查计划」，样本抽出来之后没有任何入口
  能复核——抽完就没有下文了。质控的价值在复核那一步，不在抽样那一步。
* **存疑/不通过必须写理由**：三档结论里只有"通过"可以无话可说。不写理由的
  "不通过"没法复查，等于把一条质控结论作废。
* **改随访记录不提交空串**：那些字段都是可选，空串会被当成"要改成空"——
  把计划日期清掉，而用户只是没填那一栏。
* **报表模板的改档要挂在它自己那一页**：我第一版把处理器挂到了随访页上，
  而模板表格在「智能辅助报告端」——按钮和处理器不在同一页，点了没反应。
"""
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
SPD_JS = (STATIC / "pages-spd.js").read_text(encoding="utf-8")


def _block(start_fn: str, end_fn: str) -> str:
    start = SPD_JS.index(f"async function {start_fn}(")
    end = SPD_JS.index(f"async function {end_fn}(", start)
    return SPD_JS[start:end]


FU = _block("renderSpdFollowup", "renderSpdReport")
RPT = SPD_JS[SPD_JS.index("async function renderSpdReport("):]


@pytest.mark.parametrize(
    "path",
    [
        "/context`",
        "/api/spd/health-calendar",
        "/api/spd/qc-samples?",
        "/api/spd/qc-samples/${",
        "/api/spd/followup-records/${",
        "/api/spd/followup-rules/${",
        "/api/spd/questionnaires/${",
        "/api/spd/call-tasks/${",
    ],
)
def test_随访各端点都在本页调到(path):
    assert path in FU, f"随访页没有调用 {path}"


def test_报表模板改档挂在它自己那一页():
    """第一版我把处理器挂到了随访页，而模板表格在报告端——按钮和处理器不在
    同一页，点了没反应。这条盯住两者同页。"""
    assert "data-tpl-edit" in RPT, "报表模板行没有改档入口"
    assert "/api/spd/report-templates/${" in RPT, "改档的处理器不在报告端这一页"
    assert "/api/spd/report-templates/${" not in FU, (
        "报表模板的处理器还留在随访页——按钮在另一页，点了不会有反应"
    )


def test_抽查样本可复核():
    """页面原先只有「生成抽查计划」，样本抽出来之后没有入口复核。

    **盯渲染处那一次，不是"文件里出现过"**：`data-qc-res` 在事件处理器里
    （`el4("data-qc-res")`）本来就有一份，只删按钮照样绿——这是本轮第三次踩
    同一个坑（前两次在 population 的「认领按钮」和 tasks 的「路径调整」上）。
    """
    assert 'data-qc-res="' in FU, (
        "抽查样本没有「记结论」入口——抽完就没有下文了，质控的价值在复核那一步"
    )
    assert "只负责抽样" in FU, "没说清抽样与复核是两步"


def test_存疑或不通过必须写理由():
    assert "存疑或不通过必须写明理由" in FU, (
        "三档结论里只有「通过」可以无话可说；不写理由的「不通过」没法复查"
    )


def test_改随访记录不提交空串():
    idx = FU.index("fuEdit.dataset.fuEdit")
    around = FU[max(0, idx - 400):idx]
    assert 'filter(([, v]) => v !== "")' in around, (
        "改随访记录没有过滤空串——用户只是没填那一栏，不是要把它清空"
    )


def test_呼叫结果只给未完成的任务():
    idx = FU.index('data-call-res="')
    around = FU[max(0, idx - 200):idx]
    assert 'c.status === "pending"' in around, (
        "已完成的呼叫任务也给了「记结果」按钮"
    )
