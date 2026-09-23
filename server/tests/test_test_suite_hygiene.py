"""测试套件自身的卫生守则：同名定义会**静默顶掉**前一个。

起因是本轮真踩的一脚：给 `tests/conftest.py` 加"倒序执行"时，顺手**又写了一个**
`def pytest_collection_modifyitems`。Python 只是把名字重新绑定，于是上面那个
负责"默认跳过 e2e / watchdog"的同名 hook 被整个顶掉——e2e 从"默认跳过"变成真去
拉浏览器，11 个用例当场 error。没有任何语法错误、没有任何警告。

同一个形状在普通测试模块里更隐蔽：两条用例重名，后写的顶掉先写的，**先写的那条
再也不会跑**，而套件照样全绿、计数只是少一。这正是"看不见的失效"。
"""
from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def _dup_top_level_defs(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    return sorted({n for n in names if names.count(n) > 1})


def test_没有同名的顶层定义():
    offenders = {}
    for path in sorted(TESTS.rglob("*.py")):
        dups = _dup_top_level_defs(path)
        if dups:
            offenders[str(path.relative_to(TESTS))] = dups
    assert offenders == {}, (
        f"这些文件里有重名的顶层定义，后写的会**静默顶掉**先写的：{offenders}\n"
        "  用例重名 = 先写的那条再也不会跑，而套件照样全绿；"
        "conftest 里的 hook 重名 = 前一个 hook 整个失效（本轮真踩过：e2e 不再默认跳过）。"
    )


def test_倒序开关长在唯一那个收集钩子里():
    """`MEDPLAT_TEST_REVERSE` 必须并进既有的 `pytest_collection_modifyitems`。

    单独再写一个同名 hook 会把默认跳过 e2e 的那几行顶掉——上一条用例讲的就是这个。
    这里正面钉一次：钩子只许有一个，且倒序逻辑就在它里面。
    """
    source = (TESTS / "conftest.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    hooks = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "pytest_collection_modifyitems"
    ]
    assert len(hooks) == 1, f"收集钩子有 {len(hooks)} 个，多出来的会顶掉前面的"
    body = ast.get_source_segment(source, hooks[0]) or ""
    assert "MEDPLAT_TEST_REVERSE" in body, (
        "倒序开关不在这个钩子里了——`scripts/check_test_order.py` 会变成空转："
        "它照常跑、照常全绿，但根本没有倒序过。"
    )
    assert "items.reverse()" in body
