"""`tests/astcode.py` 的单元用例 + 一条防回退的形状规则。

这份文件守的是 2026-09-10 变异审计挖出来的那个洞：静态闸门在函数源码里找守卫名
（`IntegrityError` / `assert_patient_visible` / `pii_filter` / `paginate(`），
而 `ast.unparse` / `ast.dump` **包含 docstring**——于是一句散文就能冒充守卫。
带对照组实测过：真拿掉 `except IntegrityError` 是红的；同样拿掉、docstring 里
提一句就变绿。来龙去脉见 `tests/astcode.py` 的模块 docstring。
"""
import ast
import os
import pathlib

import astcode

TESTS_DIR = pathlib.Path(__file__).resolve().parent


def _fn(src: str) -> ast.AST:
    return ast.parse(src).body[0]


def test_剥掉函数docstring():
    fn = _fn('def f():\n    """提一句 IntegrityError。"""\n    return 1\n')
    assert "IntegrityError" not in astcode.code(fn)
    assert "IntegrityError" not in astcode.dump(fn)
    assert "return 1" in astcode.code(fn)


def test_剥掉嵌套函数与类的docstring():
    fn = _fn(
        'def outer():\n'
        '    """外层提 assert_patient_visible。"""\n'
        '    class C:\n'
        '        """类里提 pii_filter。"""\n'
        '        def inner(self):\n'
        '            """内层提 paginate(。"""\n'
        '            return 2\n'
        '    return C\n'
    )
    code = astcode.code(fn)
    for token in ("assert_patient_visible", "pii_filter", "paginate("):
        assert token not in code, f"{token} 没被剥掉——嵌套层没走到"


def test_不剥别的字符串常量():
    """只剥 docstring。`detail="…"`、SQL 片段、正则字面量都可能正是判据要看的。"""
    fn = _fn('def f():\n    raise HTTPException(status_code=409, detail="已在黑名单")\n')
    assert "已在黑名单" in astcode.code(fn)


def test_空函数体剥完仍是合法AST():
    """只有一句 docstring 的函数，剥完要补 `pass`，否则 unparse 会炸。"""
    fn = _fn('def f():\n    """只有文档。"""\n')
    assert astcode.code(fn).strip().endswith("pass")


def test_不改原节点():
    """剥离必须走深拷贝——调用方往往还要用原节点取行号。"""
    fn = _fn('def f():\n    """文档。"""\n    return 1\n')
    astcode.code(fn)
    assert ast.get_docstring(fn) == "文档。"


#: 已改用共享剥离的闸门模块。**这份清单只许变长。**
#:
#: ⚠️ **说清这条规则的边界**：它按变量名（fn/func/node/f）判断"这次 unparse 的
#: 是不是一个函数节点"——也就是说，**它自己就是一条"只认一种写法"的判据**，
#: 而"只认一种写法的判据最容易悄悄失效"正是这一轮的教训。换个变量名它就看不见。
#: 留着它是因为它对**已修好的这几处回退**确实有效（零基线、变异验证过）；
#: 但一道**新**闸门若不 import astcode，本规则完全管不着——那只能靠
#: `tests/astcode.py` 的模块 docstring 与这条注释提醒下一个写闸门的人。
GATES_USING_ASTCODE = {
    "test_stage14_concurrency.py",
    "test_stage15_horizontal.py",
    "test_list_pagination_ratchet.py",
    "test_pii_query_point_guard.py",
}

#: 判据只认这两个变量名。第一版还带了 `node` 与 `f`，实测**四处误报**——
#: 那几处 unparse 的是**语句/表达式节点**（为了拼人读的位置串），
#: 与 docstring 无关。误报率高的规则会先被加豁免、再被加得没人看，最后被删掉，
#: 所以宁可窄一点：`fn`/`func` 是本仓库绑函数节点的惯用名。
_FN_VARS = {"fn", "func"}

#: 明知故犯、且写明了理由的裸调用。**只减不增。**
RAW_UNPARSE_OK = {
    # 这一处**故意**用裸 unparse：它是那条用例的前提自证——先证明
    # `record_qc_summary` 的 docstring 里确实还留着那句 `.limit(5000)`
    # （否则用例是空转），再证明 `_code(fn)` 把它剥掉了。两句一正一反，
    # 正好是本模块要守的那件事的活样本。
    "test_list_pagination_ratchet.py:484  ast.unparse(fn)",
}


def _binds_astcode(path: pathlib.Path) -> bool:
    """该模块是否真的把 `astcode` 这个名字绑进了本模块作用域。

    按 AST 判，不用 `"import astcode" in src` 那种子串判据——
    `import astcode as x` 会让子串判据通过（自审时用合成用例试出来的）。
    本模块要守的正是"判据别只认一种写法"，自己先别犯。
    """
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            if any(a.name == "astcode" and a.asname in (None, "astcode") for a in n.names):
                return True
        if isinstance(n, ast.ImportFrom) and n.module == "astcode":
            return True
    return False


def _raw_unparse_sites(path: pathlib.Path) -> list[str]:
    """该模块里仍然对「函数节点」裸调 ast.unparse / ast.dump 的位置。"""
    hits = []
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
            continue
        if not (isinstance(n.func.value, ast.Name) and n.func.value.id == "ast"):
            continue
        if n.func.attr not in ("unparse", "dump") or not n.args:
            continue
        arg = n.args[0]
        if isinstance(arg, ast.Name) and arg.id in _FN_VARS:
            hits.append(f"{path.name}:{n.lineno}  ast.{n.func.attr}({arg.id})")
    return hits


def test_已改用共享剥离的闸门不许退回裸unparse():
    """基线为空：这四个模块里不许再出现 `ast.unparse(fn)` 这种未剥 docstring 的匹配。"""
    offenders = []
    for name in sorted(GATES_USING_ASTCODE):
        path = TESTS_DIR / name
        assert path.exists(), f"{name} 不存在了——清单该更新"
        assert _binds_astcode(path), (
            f"{name} 不再把 astcode 绑进本模块——守卫名匹配多半退回了裸 unparse，"
            "那样一句 docstring 就能冒充守卫"
        )
        offenders += _raw_unparse_sites(path)
    offenders = [o for o in offenders if o not in RAW_UNPARSE_OK]
    assert offenders == [], (
        "以下位置对函数节点裸调 ast.unparse/ast.dump（未剥 docstring），"
        "散文里提一句守卫名就能冒充守卫：\n  " + "\n  ".join(offenders)
        + "\n改用 astcode.code(fn) / astcode.dump(fn)。"
    )


def test_裸调用豁免清单不得腐烂():
    """豁免项必须真的还在那儿，否则它就成了一条永远为真的空条目。"""
    live = []
    for name in sorted(GATES_USING_ASTCODE):
        live += _raw_unparse_sites(TESTS_DIR / name)
    stale = sorted(RAW_UNPARSE_OK - set(live))
    assert stale == [], f"这些豁免已不存在（行号变了或调用删了），应更新：{stale}"


def test_这条规则不是空转():
    """防空转：判据必须真的能认出裸调用，否则上面那条永远绿。"""
    probe = TESTS_DIR / "__astcode_probe__.py"
    probe.write_text(
        "import ast\n"
        "def scan(fn):\n"
        "    return ast.unparse(fn) + ast.dump(func)\n",
        encoding="utf-8",
    )
    try:
        hits = _raw_unparse_sites(probe)
        assert len(hits) == 2, f"判据认不出裸调用，实际命中 {hits}"
    finally:
        os.remove(probe)
