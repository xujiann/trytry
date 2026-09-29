"""居民端监测录入：提示与选项对得上、每个选项带单位、提交时把单位送上去（P2-738，第十九批「单位与量纲」扫描 K4-7）。

原先提示写「记录血压、血糖、体重等」，选项里却没有体重，「体质指数」也不带 kg/m²——居民想记体重只能选体质指数，填 65（kg）
就按 BMI 65 落库、进规则事实和档案；提交时从不送单位，居民端录的每一条单位都是空串，医护端清单与趋势图的单位也就空着。
"""
import re
from pathlib import Path

from app.spd.service import MEASURE_FIELDS

M_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "m" / "m.js"


def _form() -> str:
    source = M_JS.read_text(encoding="utf-8")
    start = source.index("async function renderSpdMeasure(")
    return source[start:source.index("\n}\n", start)]


def test_每个选项都在指标目录里_都带单位():
    form = _form()
    select = form[form.index('<select id="spd-metric">'):form.index("</select>")]
    options = re.findall(r'<option value="([^"]+)"([^>]*)>([^<]+)</option>', select)
    assert options, "没找到监测项选项"
    for value, attrs, label in options:
        assert value in MEASURE_FIELDS, value
        unit = re.search(r'data-unit="([^"]+)"', attrs)
        assert unit and unit.group(1) in label, (value, attrs, label)   # 修前体质指数不带单位、所有选项都没有 data-unit


def test_提交时送单位_提示不提没有的项():
    form = _form()
    assert 'unit: $("#spd-metric").selectedOptions[0].dataset.unit' in form   # 修前从不送
    hint = re.search(r'<p class="hint">([^<]+)</p>', form).group(1)
    assert "体重" not in hint, hint   # 修前提示写「体重」，选项里没有
