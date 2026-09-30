"""住院体征的体重框收得了两位小数（P2-1084，第三十一批「页面输入约束 vs 后端校验」扫描 C3-6）。

管理端体征录入与医生移动端查房的体重框写 `step="0.1"`：新生儿 3.25 kg 录不进去，浏览器拦下、提示「两个最接近的有效值
分别为 3.2 和 3.3」，只能按 100 g 取整；后端 `weight_kg` 是 0～500 的任意小数。与 P1-67「数字框收不了小数」同一口径，
改成 `step="any"`、上下界由后端判（儿童访视的体重早就改成文本框收小数）。
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_体重框不按一位小数卡步长():
    sources = {"pages-mgmt.js": 'name="weight_kg"', "m/doctor.html": 'id="rv-weight"'}
    for name, marker in sources.items():
        text = (STATIC / name).read_text(encoding="utf-8")
        (tag,) = [t for t in re.findall(r"<input[^>]*>", text) if marker in t]
        assert 'step="any"' in tag, (name, tag)   # 修前 step="0.1"
