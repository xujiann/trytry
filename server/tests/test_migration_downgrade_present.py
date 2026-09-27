"""每个迁移都实现了 `downgrade()`（CLAUDE.md §4：「每个迁移必须实现 `downgrade()`……保持这个纪录」）。

这条规矩原先只写在 CLAUDE.md 里，配着一个手写的数（「当前 94/94 全部实现」），没有任何用例盯着：新迁移漏写
`downgrade`，或者只写一句 `pass`，CI 都不会红；那个数也停在 94 不动（2026-09-27 已是 99 个迁移）。

判据：`alembic/versions` 下每个文件都在顶层定义 `upgrade` 与 `downgrade`；刻意不做事的（函数体除 docstring
外只有 `pass`，比如补偿迁移 `a1c3e5b7d9f2` 把补建的索引留给正主去删）必须在 docstring 里写明为什么——
没有理由的空函数不算实现。
"""
import ast
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _migrations() -> list[Path]:
    return sorted(VERSIONS.glob("*.py"))


def _functions(path: Path) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def _does_nothing(func: ast.FunctionDef) -> bool:
    body = func.body[1:] if ast.get_docstring(func) is not None else func.body
    return all(isinstance(stmt, ast.Pass) for stmt in body)


def test_覆盖面自证_真的遍历到了全部迁移():
    # 遍历失效（目录算错、只数到几个）时，下面两条会空转成绿——先在这里红
    assert len(_migrations()) >= 99   # 2026-09-27 实测 99 个


def test_每个迁移都定义了_upgrade_与_downgrade():
    missing = [f"{path.name}: {name}" for path in _migrations()
               for name in ("upgrade", "downgrade") if name not in _functions(path)]
    assert not missing, "迁移缺函数：\n" + "\n".join(missing)


def test_刻意不做事的_downgrade_写明了理由():
    unexplained = [path.name for path in _migrations()
                   if (func := _functions(path).get("downgrade")) is not None and _does_nothing(func)
                   and not (ast.get_docstring(func) or "").strip()]
    assert not unexplained, "downgrade 只有 pass、也没写为什么：\n" + "\n".join(unexplained)
