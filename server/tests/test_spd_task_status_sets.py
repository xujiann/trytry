"""慢专病任务的状态集合只在 `service.py` 写一份，查询里不许再手写清单（P1-127 的闸门）。

「未结束」这个集合原先在七处各写了一份，七份都漏了退回（rejected）：路径越过退回待重办的任务往下走、待办与
计数里看不到它、结案与路径取消不收它（案情见 `test_spd_task_rejected_open.py`）。**漏一个状态的清单不报错**，
只会让某一处悄悄把它当成已结束——所以不靠人记，靠两条：

- 状态集合对照列注释：`TASK_OPEN_STATUSES` ∪ `TASK_CLOSED_STATUSES` 恰是 `spd_tasks.status` 列注释列出的
  全部状态、互不相交——加了新状态没归类即红；
- 查询只认这几个名字：`app/spd` 里 `SpdTask.status.in_(…)` / `.notin_(…)` 的参数得是 `service.TASK_*`
  （或模块里直接取自它们的别名），手写的清单与自造的常量都点名；确属单个动作前置条件的写进 `ACCEPTED`、写明理由。
  条件翻转的帮手（`move_task` / `_move_or_conflict`，P2-114）同一条规矩：`expect=` 只收共享集合或单个状态字符串——
  清单藏进帮手的参数里，上面那条就看不见它了。
"""
import ast
import pathlib

SPD = pathlib.Path(__file__).resolve().parents[1] / "app" / "spd"
SHARED = {"TASK_OPEN_STATUSES", "TASK_CLOSED_STATUSES", "TASK_IN_HAND_STATUSES", "TASK_CLAIMABLE_STATUSES",
          "TASK_COMPLETABLE_STATUSES"}
#: 任务状态的条件翻转帮手（P2-114）：`expect=` 参数与 `.in_()` 同一条规矩
MOVERS = {"move_task", "_move_or_conflict"}

#: 手写清单的正当用法：`文件:取值` → 理由（只减不增；接收的前置状态原在这里，P2-83 起单条与批量共用
#: `TASK_CLAIMABLE_STATUSES`，已清空）
ACCEPTED: dict[str, str] = {}


def handwritten_status_sets(sources: dict[str, str] | None = None) -> list[str]:
    """`文件:行: 参数`——`SpdTask.status.in_/notin_` 的参数不是共享的状态集合（或直接取自它的别名）。"""
    files = {p.relative_to(SPD).as_posix(): p.read_text(encoding="utf-8")
             for p in sorted(SPD.rglob("*.py")) if "__pycache__" not in p.parts}
    files.update(sources or {})
    bad: list[tuple[str, int, str]] = []
    for rel, text in files.items():
        tree = ast.parse(text)
        aliases = {t.id for node in tree.body if isinstance(node, ast.Assign)
                   and isinstance(node.value, ast.Name) and node.value.id in SHARED
                   for t in node.targets if isinstance(t, ast.Name)}
        # 帮手之间的转发（`_move_or_conflict` 把自己的 expect 交给 `move_task`）不查：它们的调用点各自受查
        spans = [(f.lineno, f.end_lineno or f.lineno) for f in ast.walk(tree)
                 if isinstance(f, ast.FunctionDef) and f.name in MOVERS]
        for node in ast.walk(tree):
            callee = node.func.id if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) else ""
            if callee in MOVERS:
                if any(start <= node.lineno <= end for start, end in spans):
                    continue
                for kw in node.keywords:
                    ok = (isinstance(kw.value, ast.Name) and kw.value.id in SHARED | aliases) or (
                        isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str))
                    if kw.arg == "expect" and not ok:
                        bad.append((rel, node.lineno, f"{callee}(expect={ast.unparse(kw.value)})"))
                continue
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("in_", "notin_") and ast.unparse(node.func.value) == "SpdTask.status"
                    and node.args):
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Name) and arg.id in SHARED | aliases:
                continue
            if isinstance(arg, (ast.List, ast.Tuple, ast.Set)) and all(
                    isinstance(e, ast.Constant) for e in arg.elts):
                if f"{rel}:{tuple(e.value for e in arg.elts)}" in ACCEPTED:
                    continue
            bad.append((rel, node.lineno, ast.unparse(arg)))
    return [f"{rel}:{line}: {arg}" for rel, line, arg in sorted(bad)]


def test_状态集合对照列注释_每个状态都归了类():
    from app.spd.models import SpdTask
    from app.spd.service import (TASK_CLAIMABLE_STATUSES, TASK_CLOSED_STATUSES, TASK_COMPLETABLE_STATUSES,
                                 TASK_IN_HAND_STATUSES, TASK_OPEN_STATUSES)
    from test_status_text_from_backend import _column_codes

    codes = _column_codes(SpdTask, "status")
    assert "rejected" in codes and len(codes) >= 8   # 防空转：列注释真被读到了
    assert set(TASK_OPEN_STATUSES) | set(TASK_CLOSED_STATUSES) == codes, (
        f"列注释里的状态没归类：{sorted(codes - set(TASK_OPEN_STATUSES) - set(TASK_CLOSED_STATUSES))}；"
        f"归了类却不在列注释里：{sorted(set(TASK_OPEN_STATUSES) | set(TASK_CLOSED_STATUSES) - codes)}")
    assert not set(TASK_OPEN_STATUSES) & set(TASK_CLOSED_STATUSES)
    assert "rejected" in TASK_OPEN_STATUSES and "rejected" in TASK_IN_HAND_STATUSES   # 退回即回到办理人手里
    assert set(TASK_CLAIMABLE_STATUSES) == {"pending", "overdue"}   # 单条与批量接收同一前置状态（P2-83）
    # 直接办结收未结束的、唯独不收待审核的——那条只能审（P2-244）
    assert set(TASK_COMPLETABLE_STATUSES) == set(TASK_OPEN_STATUSES) - {"submitted"}
    # 在办理人手里的：未结束里去掉等审核的与已超期的（超期扫描扫它们，报告的超期另列一表）
    assert set(TASK_IN_HAND_STATUSES) == set(TASK_OPEN_STATUSES) - {"submitted", "overdue"}


def test_查询里不手写任务状态清单():
    bad = handwritten_status_sets()
    assert not bad, (
        "以下查询手写了任务状态清单（或用了自造的常量）：\n  " + "\n  ".join(bad)
        + "\n\n用 `service.TASK_OPEN_STATUSES`（未结束，含退回）/ `TASK_IN_HAND_STATUSES`（在办理人手里）/ "
        "`TASK_CLOSED_STATUSES`（已结束）。漏一个状态的清单不报错，只会让这一处悄悄把它当成已结束（P1-127）。"
    )


def test_正当用法名单不留死条目():
    files = {p.relative_to(SPD).as_posix(): p.read_text(encoding="utf-8") for p in SPD.rglob("*.py")}
    for key, reason in ACCEPTED.items():
        rel, values = key.split(":", 1)
        assert reason.strip() and rel in files, key
        assert f"SpdTask.status.in_({values})" in files[rel].replace('"', "'"), f"名单里的 {key} 已不在代码里，划掉"


def test_判据自证_手写的与自造常量点名_共享集合与别名放过():
    snippet = (
        "from ..service import TASK_OPEN_STATUSES\n"
        "OPEN = TASK_OPEN_STATUSES\n"
        "MINE = ('pending', 'claimed', 'doing')\n"
        "a = q.filter(SpdTask.status.in_(['pending', 'claimed', 'doing', 'submitted', 'overdue']))\n"
        "b = q.filter(SpdTask.status.in_(MINE))\n"
        "c = q.filter(SpdTask.status.in_(TASK_OPEN_STATUSES))\n"
        "d = q.filter(SpdTask.status.notin_(OPEN))\n"
        "e = q.filter(SpdReferralCase.status.in_(['submitted', 'rejected']))\n"
    )
    assert [f for f in handwritten_status_sets({"probe.py": snippet}) if f.startswith("probe.py")] == [
        "probe.py:4: ['pending', 'claimed', 'doing', 'submitted', 'overdue']", "probe.py:5: MINE"]


def test_判据自证_条件翻转帮手的期望态_共享集合与单个状态放过_手写清单点名():
    snippet = (
        "from ..service import TASK_IN_HAND_STATUSES\n"
        "move_task(db, 1, 'overdue', expect=TASK_IN_HAND_STATUSES)\n"
        "_move_or_conflict(db, t, 'rejected', expect='submitted')\n"
        "move_task(db, 1, 'cancelled')\n"
        "move_task(db, 1, 'overdue', expect=('pending', 'claimed', 'doing'))\n"
        "_move_or_conflict(db, t, 'doing', expect=MINE)\n"
    )
    assert [f for f in handwritten_status_sets({"probe.py": snippet}) if f.startswith("probe.py")] == [
        "probe.py:5: move_task(expect=('pending', 'claimed', 'doing'))", "probe.py:6: _move_or_conflict(expect=MINE)"]
