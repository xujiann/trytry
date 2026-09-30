"""慢专病「风险分层」柱状图按风险编码的字母序出：高危、低危、中危、极高危（P2-967，第二十七批「排名与并列」扫描 G4-8）。

后端四处 `by_risk` 都按 `risk_level` 分组、`ORDER BY risk_level`（P2-68 补的分组键排序只为结果确定），字典键序是
high < low < mid < very_high；页面四张图（卫健委工作台、专家工作台、团队工作台、成员工作台的评估统计）用 `spdPairs`
原样按 `Object.entries` 画柱，于是「风险分层」依次是高危、低危、中危、极高危。

修法：页面加 `spdRiskPairs`，按 `SPD_RISK` 的键序（低 → 中 → 高 → 极高）取值，库里的其他取值（未分级等）跟在后面；
四张图都改用它。公用的 `spdPairs` 不动（病种分布等别的图照旧按后端次序）。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
SRC = (STATIC / "pages-spd.js").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = SRC.index(f"function {name}(")
    return SRC[start:SRC.index("\n}\n", start) + 2]


def _risk_const() -> str:
    start = SRC.index("const SPD_RISK = {")
    return SRC[start:SRC.index("};", start) + 2]


def test_风险分层的图都走按严重程度排的帮手():
    uses = re.findall(r"barChart\((\w+)\([\w.]*by_risk", SRC)
    assert uses == ["spdRiskPairs"] * 4, uses   # 修前四处都是 spdPairs
    for path in STATIC.rglob("*.js"):   # 别的页面没有按 Object.entries 画 by_risk 的
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"spdPairs\([\w.]*by_risk", text), path.name
        assert not re.search(r"Object\.entries\([\w.]*by_risk", text), path.name


def test_常量按严重程度排():
    assert re.findall(r"(\w+): \[\"", _risk_const()) == ["low", "mid", "high", "very_high"]


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端函数")
def test_跑一遍_后端字母序进来_按低中高极高出():
    backend = {"high": 3, "low": 5, "mid": 4, "very_high": 1, "未分级": 2}   # 后端 ORDER BY risk_level 的键序
    script = (_risk_const() + "\n" + _function("spdRiskPairs")
              + "\nconsole.log(JSON.stringify(spdRiskPairs(JSON.parse(process.argv[1]))));")
    out = subprocess.run(["node", "-e", script, json.dumps(backend, ensure_ascii=False)],
                         capture_output=True, text=True, check=True, timeout=60).stdout
    assert json.loads(out) == [["低危", 5], ["中危", 4], ["高危", 3], ["极高危", 1], ["未分级", 2]]
    empty = subprocess.run(["node", "-e", script, "null"], capture_output=True, text=True, check=True,
                           timeout=60).stdout
    assert json.loads(empty) == []
