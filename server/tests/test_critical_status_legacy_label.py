"""危急值闭环状态三处三套文案，存量空串态在管理端与导出写「待回填」、医生端待办却按「待确认」计（P2-1026，第二十九批「前后端
取值表」扫描 E1-8）。

`exams.CRITICAL_STATUS_NAMES` 的注释原先写「措辞与危急值页、医生移动端一致」，实际三套：管理端危急值页 `CRIT_STATUS`（已通知 /
已确认 / 已处置，空串「待回填」）、医生移动端 `CRITICAL_TAGS`（待确认 / 已接收，待处置 / 已闭环，空串「待确认」）、指标下钻导出
（照危急值页写「待回填」）。存量危急报告（迁移前）的空串等同 notified——M-1 整改后确认接收两态都收、管理端「确认接收」按钮也
两态都给；写成「待回填」像是要补录数据。

修法：各端的空串都按本端 notified 那一格写（管理端与导出改成「已通知」）；医生端按接收方视角另有一套说法，是同一个状态，
注释如实写明、不强行统一。
"""
import inspect
import re
from pathlib import Path

from app.routers import exams
from app.routers.exams import CRITICAL_STATUS_NAMES

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _labels(source, name):
    body = re.search(rf"^const {name} = \{{(.*?)\}};", source, re.M | re.S).group(1)
    return {key.strip('"'): label for key, label in re.findall(r'("?\w*"?): \["([^"]+)"', body)}


def test_管理端危急值页_空串与已通知同一格_其余照后端():
    labels = _labels((STATIC / "pages-clinical.js").read_text(encoding="utf-8"), "CRIT_STATUS")
    assert labels[""] == labels["notified"]   # 修前「待回填」
    assert {k: v for k, v in labels.items() if k} == CRITICAL_STATUS_NAMES


def test_医生端_空串与待确认同一格():
    labels = _labels((STATIC / "m" / "doctor.js").read_text(encoding="utf-8"), "CRITICAL_TAGS")
    assert labels[""] == labels["notified"] == "待确认"
    assert set(labels) == set(CRITICAL_STATUS_NAMES) | {""}


def test_后端注释不再称三端一致():
    assert "措辞与危急值页、医生移动端一致" not in inspect.getsource(exams)
