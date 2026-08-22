"""两个「绩效」页面的口径标注守卫。

平台里有**两套并行的绩效评分**（见 docs/统计口径对照表.md）：

- 「绩效考核」页 `/api/performance/orgs`：指标目录 + 固定权重，**考核口径**，周期内计分；
- 「决策分析」页 `/api/analytics/performance-report`：自定义公式 + 可随时改的权重。

两个页面都写着"绩效"、都给出一个分数，且量级相近。谁把它们摆在一起比较，
得出的结论一定是错的。口径提示是这里唯一的防线——它一旦被顺手删掉，
页面看不出任何异常，所以用例来盯。
"""
import pathlib

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "app" / "static"


@pytest.fixture(scope="module")
def core_js() -> str:
    return (STATIC / "core.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def mgmt_js() -> str:
    return (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")


def test_考核页标明自己是考核口径且点名另一套不可比(core_js):
    body = core_js[core_js.index("async function renderPerformance()"):]
    body = body[: body.index("\n}\n")]
    assert "考核口径" in body, "考核页没说明自己是哪套口径"
    assert "期末综合绩效报告" in body, "考核页没点名另一套评分"
    assert "不可比" in body, "考核页没写明两套分数不可比"


def test_考核页把评分周期显示出来(core_js):
    """分数从累计改成周期口径后，不标周期的数字没法解读。"""
    body = core_js[core_js.index("async function renderPerformance()"):]
    body = body[: body.index("\n}\n")]
    assert "data.period" in body, "考核页没把 period 显示出来"


def test_分析页标明自定义公式非考核口径(mgmt_js):
    body = mgmt_js[mgmt_js.index("期末综合绩效报告"):]
    body = body[: body.index("</div>`")]
    assert "自定义公式" in body, "分析页没说明分数来自自定义公式"
    assert "绩效考核" in body, "分析页没点名另一套评分"
    assert "不可直接比较" in body, "分析页没写明两套分数不可比"


def test_运营月报导出按钮不再单说累计(core_js):
    """CSV 里业务量列是累计、绩效分列是当年——按钮只写"累计"会误导。"""
    assert '运营月报CSV（累计）' not in core_js
    assert "绩效分当年" in core_js


def test_提示文案确实落在页面html里而不是注释(core_js, mgmt_js):
    """防呆：上面几条都是文本查找，若文案被挪进 // 注释里，断言照样通过。

    这里要求它出现在 `<p class="desc">` 之后——注释里不会有这种结构。
    """
    for text in (core_js, mgmt_js):
        idx = text.index("不可比") if "不可比" in text else text.index("不可直接比较")
        window = text[max(0, idx - 400):idx]
        assert '<p class="desc">' in window, "口径提示不在渲染出来的 HTML 里"
        assert not any(
            line.strip().startswith("//")
            for line in window.splitlines()[-1:]
        ), "口径提示被注释掉了"
