"""前端 `prompt()` 录入的棘轮（P2-38）。

`prompt()` 录不了多字段、没有校验提示、粘不了长文本，是"界面有了、功能没通"的记号——
功能完善规则 §1 第 7 项"录入不靠 `prompt()`"。存量按模块批次换成页内表单
（`spdModal` / 卡片内输入），这里只管一件事：**只许变少**。

在此之前这个数字只写在 TECH_DEBT 与规则文档里（"134 处"），没有任何东西盯着——
2026-09-24 实数是 97 行，规则文档里立规则时（2026-09-15）写下的 134 早已过期。

口径与 TECH_DEBT P2-38 一直用的一致：`app/static` 下全部 `.js` 里**含 `prompt(` 的行数**
（不是调用次数，一行两处算一行）。注释里写出这个字面量也会被数进去——注释请换个说法，
别让计数失真。
"""
from __future__ import annotations

import pathlib

STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "static"

#: 2026-09-24 孕产妇页一批换完后的行数（97 → 87）。**只许调小**：换掉一批就同步改小，
#: 降了不改也红——不改小，就给了下一次偷偷加回来的余地。
BASELINE = 87


def prompt_lines() -> list[str]:
    """含 `prompt(` 的行，`相对路径:行号`。"""
    out = []
    for path in sorted(STATIC.rglob("*.js")):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "prompt(" in line:
                out.append(f"{path.relative_to(STATIC).as_posix()}:{no}")
    return out


def test_判据自证_扫得到管理端与移动端两处目录():
    files = {line.split(":")[0] for line in prompt_lines()}
    assert any("/" not in f for f in files), "管理端 app/static/*.js 一处都没扫到，扫描面不对"
    assert any(f.startswith("m/") for f in files), "移动端 app/static/m/*.js 一处都没扫到，扫描面不对"


def test_prompt录入只许变少():
    lines = prompt_lines()
    assert len(lines) <= BASELINE, (
        f"含 prompt( 的行从 {BASELINE} 涨到 {len(lines)}：新代码不许再用 prompt() 录入，"
        "改用 spdModal 或页内表单（功能完善规则 §1 第 7 项）。\n  " + "\n  ".join(lines)
    )
    assert len(lines) == BASELINE, (
        f"含 prompt( 的行降到了 {len(lines)}，请把 BASELINE 从 {BASELINE} 改小到 {len(lines)}"
        "（棘轮只进不退），并同步 docs/闸门现状.md（跑 scripts/dump_gate_status.py）"
    )
