"""需求对照表把四条「趋势」算在没有时间序列的接口上（P2-1337，第三十九批「趋势、环比与同比」扫描 AC1-5）。

《需求对照表》是投标响应与验收核对用的（P2-561、P2-944）。卫健端 #1「随访干预评估趋势」、#3「工作成果趋势」、成员端 #1
「工作量趋势」的实现栏只写了工作台接口——卫健、团队两个工作台的出参全是时点合计（人数、任务数、完成率、超期数），页面上也
没有趋势面板；患者端 #7「按日/周/月展示……趋势」写「聚合口径见 `GET /api/spd/measurements/trend`」，那是员工端接口，居民
令牌调不了，居民端「监测」页只列最新 30 条原始读数。照着表验收的人找不到这几处趋势。

修法：四条如实标「未交付」（同形先例 P2-944；要不要补工作台趋势面板与居民端周 / 月趋势由业务定）。本用例钉住：需求里写
「趋势」的行，实现栏要么引用真有按期序列的接口，要么写明未交付；患者端走居民令牌（见对照表该节开头），员工端接口不算。
工作台或居民端监测接口哪天真给了按期序列，后两条会提醒回来改对照表。
"""
import inspect
import typing
from pathlib import Path

from pydantic import BaseModel

MATRIX = Path(__file__).resolve().parents[2] / "docs" / "全域慢专病全流程管理系统_需求对照表.md"

#: 真有按期序列的接口：指标趋势按日 / 周 / 月出点（`MeasurementTrendOut.points`），报告实例的「服务质量 / 运行趋势」段落
#: 按期出柱（`spd/reporting.py::_followup_trend` 的 `series`）。两个都不是居民端接口
SERIES_APIS = ("/api/spd/measurements/trend", "/api/spd/report-instances")
PORTAL_SECTION = "患者移动端"
#: 出参里像按期序列的字段名
SERIES_WORDS = ("trend", "series", "points", "daily", "weekly", "monthly", "history")


def _rows():
    """(所在一节的标题, 需求要点, 实现栏)。实现栏里有 `POST|GET` 这样的竖线，切开后把剩下的几段拼回去。"""
    section = ""
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            section = line
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0].isdigit():
            yield section, cells[1], "|".join(cells[2:])


def _models_in(annotation):
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    for arg in typing.get_args(annotation):
        yield from _models_in(arg)


def _field_names(model: type[BaseModel], seen: set | None = None) -> set[str]:
    """出参模型里（含嵌套模型）的全部字段名。"""
    seen = set() if seen is None else seen
    if model in seen:
        return set()
    seen.add(model)
    names = set()
    for name, field in model.model_fields.items():
        names.add(name)
        for sub in _models_in(field.annotation):
            names |= _field_names(sub, seen)
    return names


def test_写着趋势的条目_实现栏要么引用真有序列的接口_要么写明未交付():
    checked = []
    for section, requirement, implemented in _rows():
        if "趋势" not in requirement:
            continue
        checked.append(requirement[:16])
        portal = PORTAL_SECTION in section
        series = [api for api in SERIES_APIS
                  if api in implemented and (not portal or api.startswith("/api/portal/"))]
        assert series or "未交付" in implemented, (
            f"「{requirement[:30]}…」的趋势{'（居民端，员工端接口不算）' if portal else ''}没有按期序列可引，"
            f"实现栏却没写明未交付：{implemented}")
    assert len(checked) >= 5, checked   # 卫健端 #1 / #3、成员端 #1、患者端 #7、智能辅助端 #1


def test_白名单里的接口确实出按期序列():
    from app.spd import reporting
    from app.spd.routers.care import MeasurementTrendOut

    assert {"granularity", "points"} <= set(MeasurementTrendOut.model_fields)
    assert '"series"' in inspect.getsource(reporting._SECTIONS["trend"])


def test_引用的工作台出参确实没有按期序列_有了就回来改对照表():
    from app.spd.routers.workbench import HealthCommissionWorkbenchOut, TeamWorkbenchOut

    for model in (HealthCommissionWorkbenchOut, TeamWorkbenchOut):
        names = _field_names(model)
        assert {"tasks", "by_risk"} <= names, (model.__name__, names)   # 防呆：真的取到了嵌套字段
        hits = sorted(n for n in names if any(w in n for w in SERIES_WORDS))
        assert not hits, f"{model.__name__} 有了 {hits}：对照表卫健端 #1 / #3、成员端 #1 的「未交付」要随之改写"


def test_居民端监测接口确实只有原始读数_有了聚合就回来改对照表():
    from app.spd.routers.portal import SpdMeasurementOut, router

    names = set(SpdMeasurementOut.model_fields)
    assert {"value", "level", "measured_at"} <= names
    assert not names & {"avg", "min", "max", "count", "granularity", "points", "label"}, names
    assert not [r.path for r in router.routes if any(w in r.path for w in SERIES_WORDS)], (
        "居民端有了趋势接口：对照表患者端 #7 的「未交付」要随之改写")
