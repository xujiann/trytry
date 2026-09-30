"""用户手册写的住院收费顺序与代码一致：先结算、后出院（P2-915，第二十五批「费用与业务状态」扫描 J3-6）。

手册原先写「入院登记/床位安排 → 出院办理 → 费用结算页生成结算单」；代码要求先结算后出院（`_assert_billing_settled`：
存在未结清住院费用，结算后方可出院）——照手册先办出院只得 409。修后手册按代码写：计费 → 结算（押金冲抵）→ 补缴 /
退押金 → 出院办理。
"""
from pathlib import Path

MANUAL = (Path(__file__).resolve().parents[2] / "docs" / "用户手册.md").read_text(encoding="utf-8")


def test_住院与结算_先结算后出院():
    start = MANUAL.index("**住院与结算**")
    step = MANUAL[start:MANUAL.index("4. **", start)]
    assert step.index("结算单") < step.index("出院办理"), step   # 修前出院办理在前
    assert "按**住院号**计费" in step
