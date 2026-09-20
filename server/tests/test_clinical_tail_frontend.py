"""医嘱执行 / 传染病报告卡 / 抢救转归 / 缺药结案 / DRG 事前事中 / 同意撤回 /
慢病风险与病种目录 / 老年统计 的界面入口守卫（P1-40，八模块清零）。

这一批里最要紧的三条，共同点是**默认值会替人做一个医疗结论**：

* **皮试结果是三态**：不传＝这条医嘱不需要皮试，`negative`/`positive` 是做过了。
  做成勾选框（只有真/假）会把"没做皮试"记成"阴性"——这是要出人命的那类默认值。
* **抢救转归只有成功/无效两种结论**，"还没下结论"必须留空：写进来会把抢救
  成功率算低。而且只对**已到院**的病例判定——车还在路上就写"抢救成功"，
  这个指标就没有可信度了。
* **DRG 事前提示给的是候选组，不是结论**：报一个确定的组会让人照着组去写
  诊断，那是把 DRG 用反了；未命中也如实说"未匹配"，不落兜底组。

其余各自的判据：

* 传染病报告卡的三个及时性字段在"病种不在目录/发病日期非法"时是 null，
  照实显示"无法判定"——把 null 渲染成"及时"等于替上报人做了个没根据的结论。
* 缺药的取药/未取药只能在配送到位之后判定（后端 409），取消则任何阶段都行。
* 同意撤回不删行，只置 `revoked_at`——撤回本身也要可举证。
* 同意文本版本库要能看历史版本：举证以记录里的 `text_version` 为准，
  只留生效版的话，拿旧版本签的同意就核对不回去了。
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
SRC = {n: (STATIC / n).read_text(encoding="utf-8") for n in
       ("core.js", "pages-clinical.js", "pages-public.js")}


def _block(file: str, fn: str) -> str:
    src = SRC[file]
    start = src.index(fn)
    rest = src[start + len(fn):]
    marker = "\nasync function render"
    return src[start:start + len(fn) + rest.index(marker)] if marker in rest else src[start:]


INP = _block("pages-clinical.js", "async function renderInpatient()")
INF = _block("pages-clinical.js", "async function renderInfectious()")
EM = _block("pages-clinical.js", "async function renderEmergency()")
MED = _block("pages-clinical.js", "async function renderMedication()")
CONS = _block("pages-clinical.js", "async function renderConsents()")
ELD = _block("pages-clinical.js", "async function renderEldercare()")
DRG = _block("pages-public.js", "async function renderDrgs()")
CHR = _block("core.js", "async function renderChronic()")

PAGES = {"inp": INP, "inf": INF, "em": EM, "med": MED, "cons": CONS, "eld": ELD,
         "drg": DRG, "chr": CHR}


@pytest.mark.parametrize(
    ("page", "path"),
    [
        ("inp", "/api/inpatient/orders/${d.ordexec}/executions"),
        ("inp", "/api/inpatient/orders/${d.ordexecs}/executions"),
        ("inf", "/api/infectious/cases/export.csv?"),
        ("inf", "/api/infectious/cases/${infcard}/report-card"),
        ("em", "/api/emergency/cases/${rescue}/rescue-outcome"),
        ("med", "/api/medication/shortages/stats"),
        ("med", "/api/medication/shortages/${shclose}/close"),
        ("drg", "/api/drgs/pre-check"),
        ("drg", "/api/drgs/in-stay-alerts"),
        ("cons", "/api/consents/texts?"),
        ("cons", "/api/consents/${revoke}/revoke"),
        ("chr", "/api/chronic/${crisk}/risk"),
        ("chr", "/api/chronic/disease-types/${dtcycle}"),
        ("chr", "/api/chronic/disease-types/${dttoggle}"),
        ("eld", "/api/eldercare/stats"),
    ],
)
def test_端点在对应页面上有调用点(page, path):
    assert path in PAGES[page], f"{page} 页面上找不到 {path} 的调用点"


# ------------------------------------------------- 默认值会替人下结论的那三条

def test_皮试结果是三态不是勾选框():
    """不传＝本医嘱不需要皮试；做成两态会把"没做"记成"阴性"。"""
    assert '"没做皮试"记成"阴性"' in INP
    assert 'body.skin_test_result = "negative"' in INP
    assert 'body.skin_test_result = "positive"' in INP
    # 留空时必须**不带**这个键
    seg = INP[INP.index("if (d.ordexec)"):]
    assert 'const body = { note:' in seg
    assert re.search(r'if \(skin\.trim\(\) === "1"\)', seg), "没有显式的三分支"


def test_皮试阳性与阴性在记录里可区分():
    assert '"阳性" : "阴性"' in INP
    assert 'x.skin_test_result === null ? "—"' in INP, "没做皮试的显示成了阴性"


def test_抢救转归只对已到院病例给出按钮():
    """车还在路上就写"抢救成功"，这个指标就没有可信度了。"""
    assert '["arrived", "admitted"].includes(c.status)' in EM
    assert "data-rescue=" in EM


def test_抢救转归没下结论就不提交():
    """"没救过来"和"还没下结论"必须分开，后者留空。"""
    seg = EM[EM.index("if (rescue)"):]
    assert 'pick === "1" ? "success" : pick === "2" ? "failed" : null' in seg
    assert "if (!outcome) return;" in seg
    assert "留空不是一种结论" in seg


def test_DRG事前提示说明是候选不是结论():
    assert "而不是一个结论" in DRG
    assert "不落兜底组" in DRG
    assert "r.matched" in DRG, "未命中没有单独的分支"


# ------------------------------------------------- 其余判据

def test_报告卡的及时性为null时显示无法判定():
    """目录外病种/发病日期非法时三个字段都是 null。"""
    assert "card.report_hours === null" in INF
    assert "card.late === null" in INF
    assert "无法判定" in INF


def test_报告卡与导出都写明无直报专线():
    assert "无网络直报专线" in INF
    assert "手工网报" in INF
    assert "不虚构未存储的字段" in INF


def test_缺药结案按状态给按钮():
    """取药/未取药只能在配送到位之后判定（后端 409）；取消任何阶段都行。"""
    assert 's.status === "delivered"' in MED
    assert '["registered", "purchasing", "delivered"].includes(s.status)' in MED


def test_缺药状态词表覆盖三个终态():
    """statusTag 对没有文案的状态解构 undefined，整页白屏——不是这一行降级。"""
    from app.routers.medication import _SHORTAGE_CLOSED

    for status in _SHORTAGE_CLOSED:
        assert f"{status}: [" in MED, f"终态 {status} 没有文案，这一页会白屏"


def test_缺药履约率分母口径写在界面上():
    assert "sstats.fulfillment_rate_pct" in MED
    assert "sstats.caliber" in MED


def test_同意撤回不删行():
    assert "撤回<b>不删行</b>" in CONS
    assert "data-revoke=" in CONS
    seg = CONS[CONS.index("if (revoke)"):]
    assert "confirm(" in seg


def test_同意文本能看历史版本():
    """举证以记录里的 text_version 为准，只留生效版就核对不回去了。"""
    assert "含历史版本" in CONS
    assert 'q.set("active_only", "false")' in CONS


def test_慢病风险的数据不足不当作平稳():
    """少于 2 次有效随访是 insufficient_data，不是 stable。"""
    assert "insufficient_data:" in CHR
    assert "没数据和没变化是两回事" in CHR


def test_病种停用不影响已建档案():
    assert "已建档案照旧" in CHR
    assert "不回溯" in CHR, "改周期没说明不回溯已排好的随访日"


def test_老年统计未筛查单列():
    """按 0 分并入会把"没做"读成"分数为 0"。"""
    assert "stats.cognitive.unscreened" in ELD
    assert "不按 0 分并入" in ELD
    assert "stats.caliber" in ELD
