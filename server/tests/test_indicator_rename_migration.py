"""迁移 b5d9f3a71c2e：指标名过时**只报告、不替人改**。

起初这条迁移写的是 `UPDATE performance_indicators SET name = ...`（带
`AND name = 旧名` 以免冲掉现场改过的名字）。`test_migration_data_safety.py`
把它判为 A 档拦下了，拦得对——而且理由不止"规则如此"：

`performance_indicators.name` 是**管理端可编辑**的现场配置，会出现在各县自己的
考核文件、报表标题、对上汇报材料里。迁移把它悄悄改掉，现场看到的是"报表列名
自己变了"，库里却没有任何记录说明是谁改的。`AND name = 旧名` 只能挡住"已经改过名"
的库，挡不住"没改过名但引用了这个名字"的库。

所以改成平台通则的形状（与 `a1c3e5b7d9f2` / `e5b7c9d1f3a4` 同源）：探到就报告，
处置交人工。本文件盯的就是"它真的不改数据"这一条。
"""
import pathlib
import re

MIGRATION_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic" / "versions" / "b5d9f3a71c2e_rename_remote_exam_indicator.py"
)
SOURCE = MIGRATION_PATH.read_text(encoding="utf-8")

OLD_NAME = "远程诊断服务量"
NEW_NAME = "共享诊断协同量"


def _code_without_docstring() -> str:
    """去掉模块 docstring——处置 SQL 写在那里是**对的**，不该被下面的断言误伤。

    按 AST 节点的行号切。不要拿 `ast.get_docstring()` 的返回值去拼三引号再
    `SOURCE.replace()`：它会把缩进规整化，拼回去的串跟源码对不上，replace
    静默不生效——"去掉了 docstring" 就成了假的，断言其实跑在完整源码上。
    （第一版正是这么写的，被本迁移 docstring 里那句"起初这里写的是 UPDATE …"
    当场抓出来。）
    """
    import ast

    tree = ast.parse(SOURCE)
    node = tree.body[0] if tree.body else None
    if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)):
        return SOURCE
    lines = SOURCE.splitlines(keepends=True)
    return "".join(lines[node.end_lineno:])


def _executed_sql() -> list[str]:
    """取出**真正会被执行**的 SQL 字面量：`sa.text(...)` / `*.execute(...)` 的实参。

    不做"源码里出现 UPDATE 就算"的粗判——这个文件的注释与 docstring 本来就要
    解释"为什么不写 UPDATE"，粗判会把解释本身判成违规（第一版就是这么翻车的）。
    要断言的是执行面，那就只看执行面。
    """
    import ast

    found: list[str] = []
    for node in ast.walk(ast.parse(SOURCE)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name not in {"text", "execute"}:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.append(arg.value)
            elif isinstance(arg, ast.Name):        # 引用了模块级常量
                for top in ast.parse(SOURCE).body:
                    if (isinstance(top, ast.Assign)
                            and any(getattr(t, "id", "") == arg.id for t in top.targets)):
                        found.append(ast.unparse(top.value))
    return found


def test_执行面只有只读语句():
    """核心断言：这条迁移执行的 SQL 里没有任何写操作。"""
    executed = _executed_sql()
    assert executed, "一条 SQL 都没解析到，本用例失去区分力——解析方式坏了"
    for sql in executed:
        upper = sql.upper()
        for verb in ("UPDATE", "DELETE", "INSERT", "DROP", "ALTER"):
            assert verb not in upper, (
                f"迁移执行了写操作（{verb}）：{sql[:120]}——"
                "这条迁移的全部职责是报告，不是改数据"
            )
        assert "SELECT" in upper, f"执行了非 SELECT 的语句：{sql[:120]}"


def test_探测语句是只读的():
    assert "SELECT id, name FROM performance_indicators" in SOURCE
    assert "WHERE key = 'remote_exam'" in SOURCE


def test_探到旧名要打日志且指名到行():
    """只说"有问题"没用，运维得知道是哪一行、该怎么办。"""
    assert "logger.warning" in SOURCE
    assert "[tuple(r) for r in rows]" in SOURCE, "日志没带上定位信息"
    assert "docstring" in SOURCE, "日志没告诉人去哪儿找处置办法"


def test_没探到就不吭声():
    """已经是新名的库不该每次升级都刷一条 WARNING。"""
    assert re.search(r"if not rows:\s*\n\s*return", SOURCE), (
        "缺少'没有陈旧行就直接返回'的短路——否则干净的库每次升级都被刷屏"
    )


def test_docstring里给了可直接执行的处置SQL():
    """报告式迁移的价值全在这份处置说明上，缺了它就只是在抱怨。"""
    import ast

    doc = ast.get_docstring(ast.parse(SOURCE)) or ""
    assert "UPDATE performance_indicators" in doc
    assert NEW_NAME in doc and OLD_NAME in doc
    assert "key = 'remote_exam'" in doc
    assert "管理端" in doc, "没告诉人还可以在界面上改"
    assert "不影响计分" in doc, "没说明不改的后果——运维会以为这是必须做的"


def test_downgrade也只报告():
    code = _code_without_docstring()
    body = code[code.index("def downgrade()"):]
    assert "_report(" in body
    assert "op.execute" not in body


def test_守卫本身没瞎():
    """`_code_without_docstring` 抓错的话，上面几条会在近乎空串上恒真。"""
    code = _code_without_docstring()
    assert "def upgrade()" in code and "logger" in code
    assert len(code) > 500, f"去掉 docstring 后只剩 {len(code)} 字符，剥离方式不对"
    assert OLD_NAME in code, "常量应当仍在代码里"
