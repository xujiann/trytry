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

#: 只许调小：换掉一批就同步改小，降了不改也红——不改小，就给了下一次偷偷加回来的余地。
#: 97 → 87（2026-09-24 孕产妇页）→ 77（同日人财物页）→ 66（同日手术页 + 医生移动端手术页签）
#: → 59（同日物资页）→ 54（同日住院页）→ 50（同日门急诊文书页）→ 46（同日绩效改进任务）
#: → 43（同日集中审方）→ 40（同日互联网+诊疗）→ 37（同日病理标本）
#: → 32（同日危急值处置反馈 + 医生移动端，移动端清零）→ 31（同日开单前互认）
#: → 29（同日流程引擎推进 / 终止）→ 28（同日更正 / 注销申请审核）→ 26（同日双通道申报审核）
#: → 24（同日公卫事件处置记录）→ 22（同日室内质控失控处理）→ 20（同日家医签约履约）
#: → 18（同日急救绿道录节点）→ 16（同日上门服务派单 / 完成）→ 13（同日适宜技术实训考核）
#: → 12（同日随访中心完成随访）→ 11（同日人员下沉职称等级）→ 10（同日接种禁忌解除）。
BASELINE = 10


def prompt_lines(root: pathlib.Path = STATIC) -> list[str]:
    """含 `prompt(` 的行，`相对路径:行号`。"""
    out = []
    for path in sorted(root.rglob("*.js")):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "prompt(" in line:
                out.append(f"{path.relative_to(root).as_posix()}:{no}")
    return out


def test_判据自证_扫得到管理端与移动端两处目录(tmp_path):
    """扫描面要真的覆盖管理端与移动端两处目录。

    原先的自证是"现存的 `prompt(` 行里两处目录都有"——移动端 2026-09-24 清零以后这就不成立了，
    而清零恰恰是这把棘轮要的结果。改成按真实布局在临时目录里各植一行、两处都得扫到，
    再确认真实目录下两处都有被扫的 `.js`（扫描面没有因为改路径之类的事悄悄缩小）。
    """
    (tmp_path / "m").mkdir()
    (tmp_path / "a.js").write_text('const x = prompt("管理端");\n', encoding="utf-8")
    (tmp_path / "m" / "b.js").write_text('const y = prompt("移动端");\n', encoding="utf-8")
    assert prompt_lines(tmp_path) == ["a.js:1", "m/b.js:1"], "植进去的两行没有都扫到，判据空转"
    scanned = {p.relative_to(STATIC).parts[0] for p in STATIC.rglob("*.js")}
    assert "m" in scanned, "移动端 app/static/m/*.js 不在扫描面里"
    assert any(part.endswith(".js") for part in scanned), "管理端 app/static/*.js 不在扫描面里"


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
