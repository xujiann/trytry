"""医生移动端慢病随访录完一条，档案下拉悄悄跳回第一条：补一条就记进了别人的档案（P2-1011，第二十九批「页面状态残留」扫描 E2-4）。

`loadChronic` 录完即重画下拉、不带 `selected`，清单按分级倒序，于是跳回分级最高的第一条，指标框按那一份的病种重画；选项只写
「档案N · 病种 · 级别」，看不出换了人。真浏览器实测：选档案 2 录 128/80，录完下拉是档案 1，再补一条「低盐饮食」记进了档案 1。
同文件查房 `loadRound` 是「选中的那条还在就留着」。

修法：重画前记下选中的那一份、重画时带上 `selected`；选项写上患者号。端到端用例在 `tests/e2e/test_flows.py`。
"""
from pathlib import Path

from jssrc import strip_comments

SRC = strip_comments((Path(__file__).resolve().parents[1] / "app" / "static" / "m" / "doctor.js").read_text(encoding="utf-8"))


def test_重画随访档案下拉时留住选中的那一份():
    start = SRC.index("async function loadChronic()")
    body = SRC[start:SRC.index("\n}\n", start)]
    assert body.index('const picked = $("#fu-chronic").value;') < body.index('$("#fu-chronic").innerHTML = ')
    assert 'String(c.id) === picked ? " selected" : ""' in body
    assert "患者${esc(c.patient_id)}" in body
    # 指标框在下拉重画之后按选中的那一份画
    assert body.index('$("#fu-chronic").innerHTML = ') < body.index("renderMetricInputs();")
