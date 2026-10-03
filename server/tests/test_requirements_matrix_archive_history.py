"""需求对照表患者端 #3 把既往史、过敏史、家族史算在 `GET /api/portal/spd/archive` 头上（P2-1224，第三十五批「用药安全校验的
覆盖面」扫描 T4-1 的 clear 部分）。

《需求对照表》是投标响应与验收核对用的（P2-561、P2-944）。#3 写「展示基本资料、生活习惯、既往史、过敏史、家族史……按时间线
汇聚门诊、住院、随访、检验、影像与诊断报告」，实现栏只写了慢专病档案接口；那个接口回的是基本资料、各病种档案的生活习惯 /
危险因素 / 并发症 / 标签 / 风险等级，时间线只并了就诊与随访——平台根本没有过敏史（全仓没有过敏史表或字段），既往史、家族史
也没有结构化数据，检验、影像报告在平台居民端档案另一个接口里。

修法：实现栏如实写明哪些有、在哪个接口，既往史 / 过敏史 / 家族史标「未交付」（要不要建过敏史随待裁定定）。本用例钉住：
这一行不再把三项算成已实现；接口哪天真的给了这几项，这条会提醒回来改对照表。
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "docs" / "全域慢专病全流程管理系统_需求对照表.md"
PORTAL = ROOT / "server" / "app" / "spd" / "routers" / "portal.py"
HISTORY = ("既往史", "过敏史", "家族史")


def _archive_row():
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0].isdigit() and "过敏史" in cells[1] and "/api/portal/spd/archive" in cells[2]:
            return cells[1], cells[2]
    raise AssertionError("需求对照表里找不到写着过敏史、实现为 /api/portal/spd/archive 的那一行")


def _archive_keys() -> set[str]:
    """`archive` 函数返回体里出现的全部字符串键（含嵌套的 profiles / patient）。"""
    tree = ast.parse(PORTAL.read_text(encoding="utf-8"))
    func = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "archive")
    keys = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Dict):
            keys |= {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return keys


def test_既往史过敏史家族史标为未交付():
    _, implemented = _archive_row()
    assert "未交付" in implemented, implemented   # 修前实现栏只写接口名，等于三项都算已实现
    clause = implemented[implemented.index("未交付") - 40:implemented.index("未交付") + 10]
    for word in HISTORY:
        assert word in clause, (word, clause)


def test_检验影像报告指到实际给出它们的接口():
    _, implemented = _archive_row()
    assert "/api/portal/me/archive" in implemented and "exam_reports" in implemented, implemented


def test_接口确实没给这几项_给了就回来改对照表():
    keys = _archive_keys()
    assert {"patient", "profiles", "timeline"} <= keys, keys
    for key in keys:
        assert not any(w in key for w in ("allerg", "past_history", "family_history")), (
            f"慢专病档案接口给出了 {key}：对照表 #3 的「未交付」要随之改写")
