"""状态取值只有一份真源：列上方那行注释（P2-623）。

CLAUDE.md §4：「状态：裸字符串，不用 Enum；取值范围写在列注释与路由 pattern 里」。可这条规矩一直没有东西盯着——
取值一旦写岔（`"success"` / `"succeeded"` 那种，CLAUDE.md §12 点过名的 `monitor.py` 就是这么出的事），比较永远为假、
筛选永远是空的，不报错、不变红。2026-09-27 全仓实测：状态列里只有两列没写取值注释（量表、定时任务的最近一次结果），
代码读写的取值全部对得上。趁干净立成零基线：

1. 字符串状态列（名叫 `status` 或以 `_status` 结尾）上方必须有一行「取值=中文, …」注释；
2. 按模型读写这一列的字面量（`Model.status == "x"` / `.in_([...])` / `update({Model.status: "x"})` /
   `Model(status="x")` / `move_row(db, Model, …, status="x")`，模块级字符串常量元组会被展开）必须在这一列的注释或默认值里；
3. 实例上比较或赋值的字面量（`obj.status == "x"`、`obj.status = "x"`，认不出是哪个模型）必须至少在某一列的注释里出现过。
"""
import ast
import collections
import glob
import os
import pathlib
import re

SERVER = pathlib.Path(__file__).resolve().parents[1]
MODEL_FILES = sorted(glob.glob(str(SERVER / "app" / "models" / "*.py"))) + [str(SERVER / "app" / "spd" / "models.py")]

_COLUMN = re.compile(r"\s+(status|\w+_status): Mapped\[str(?: \| None)?\] = mapped_column\((.*)")
_VALUE = re.compile(r'(?<![\w"])([a-z][a-z0-9_]*)\s*=')


def documented(model_sources: dict[str, str] | None = None) -> tuple[dict, dict]:
    """{(模型, 列): 注释写明的取值集合}，{(模型, 列): 默认值}。注释是紧挨在列上方的连续 `#` 行。"""
    if model_sources is None:
        model_sources = {f: open(f, encoding="utf-8").read() for f in MODEL_FILES}
    doc: dict[tuple[str, str], set[str]] = {}
    default: dict[tuple[str, str], str] = {}
    for src in model_sources.values():
        lines = src.split("\n")
        cls = None
        for i, line in enumerate(lines):
            m = re.match(r"class (\w+)\(", line)
            if m:
                cls = m.group(1)
            m = _COLUMN.match(line)
            if not (m and cls):
                continue
            j, comment = i - 1, []
            while j >= 0 and lines[j].strip().startswith("#"):
                comment.insert(0, lines[j].strip())
                j -= 1
            text = " ".join(comment)
            values = set(_VALUE.findall(text)) | ({""} if '""=' in text else set())
            doc[(cls, m.group(1))] = values
            dm = re.search(r'default="([^"]*)"', line)
            if dm:
                default[(cls, m.group(1))] = dm.group(1)
    return doc, default


def _code_files() -> list[str]:
    files = glob.glob(str(SERVER / "app" / "**" / "*.py"), recursive=True)
    return sorted(p for p in files if os.sep + "models" + os.sep not in p and not p.endswith(os.sep + "models.py"))


def _str_set(node: ast.AST) -> set[str] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        vals = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        return set(vals) if len(vals) == len(node.elts) else None
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in ("frozenset", "set", "tuple") and node.args):
        return _str_set(node.args[0])
    return None


def usages(code_sources: dict[str, str], doc: dict) -> tuple[dict, dict, dict]:
    """（按模型读的, 按模型写的, 实例层的）：{键: {取值: {位置}}}；实例层的键是属性名。"""
    trees = {path: ast.parse(src) for path, src in code_sources.items()}
    constants: dict[str, set[str]] = collections.defaultdict(set)   # 模块级字符串常量（同名多处定义取并集）
    for tree in trees.values():
        for n in tree.body:
            if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
                values = _str_set(n.value)
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                for t in targets:
                    if values is not None and isinstance(t, ast.Name):
                        constants[t.id] |= values

    def resolve(node: ast.AST) -> set[str]:
        values = _str_set(node)
        if values is not None:
            return values
        return set(constants.get(node.id, ())) if isinstance(node, ast.Name) else set()

    columns = collections.defaultdict(set)
    for model, col in doc:
        columns[model].add(col)

    def column(node: ast.AST) -> tuple[str, str] | None:
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.attr in columns.get(node.value.id, ())):
            return node.value.id, node.attr
        return None

    reads = collections.defaultdict(lambda: collections.defaultdict(set))
    writes = collections.defaultdict(lambda: collections.defaultdict(set))
    instance = collections.defaultdict(lambda: collections.defaultdict(set))
    for path, tree in trees.items():
        rel = os.path.relpath(path, SERVER)
        for n in ast.walk(tree):
            where = f"{rel}:{getattr(n, 'lineno', 0)}"
            if isinstance(n, ast.Compare):
                key = column(n.left)
                for comp in n.comparators:
                    for value in resolve(comp):
                        if key:
                            reads[key][value].add(where)
                        elif isinstance(n.left, ast.Attribute) and (n.left.attr == "status" or n.left.attr.endswith("_status")):
                            instance[n.left.attr][value].add(where)
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Attribute) and (t.attr == "status" or t.attr.endswith("_status")):
                        for value in (_str_set(n.value) or ()):
                            instance[t.attr][value].add(where)
            elif isinstance(n, ast.Dict):
                for k, v in zip(n.keys, n.values):
                    key = column(k) if k is not None else None
                    for value in (resolve(v) if key else ()):
                        writes[key][value].add(where)
            if isinstance(n, ast.Call):
                if isinstance(n.func, ast.Attribute) and n.func.attr in ("in_", "notin_") and n.args:
                    key = column(n.func.value)
                    for value in (resolve(n.args[0]) if key else ()):
                        reads[key][value].add(where)
                name = n.func.id if isinstance(n.func, ast.Name) else (n.func.attr if isinstance(n.func, ast.Attribute) else "")
                model = name if name in columns else None
                if name in ("move_row", "_move_row") and len(n.args) >= 2 and isinstance(n.args[1], ast.Name):
                    model = n.args[1].id if n.args[1].id in columns else None
                for kw in (n.keywords if model else ()):
                    if kw.arg in columns[model]:
                        for value in resolve(kw.value):
                            writes[(model, kw.arg)][value].add(where)
    return reads, writes, instance


def _allowed(doc: dict, default: dict, key: tuple[str, str]) -> set[str]:
    return doc[key] | ({default[key]} if key in default else set())


def _all() -> tuple[dict, dict, dict, dict, dict]:
    doc, default = documented()
    code = {p: open(p, encoding="utf-8").read() for p in _code_files()}
    reads, writes, instance = usages(code, doc)
    return doc, default, reads, writes, instance


def _bare(doc: dict) -> list[str]:
    return sorted(f"{m}.{c}" for (m, c), values in doc.items() if not values)


def _bad_reads_writes(doc: dict, default: dict, reads: dict, writes: dict) -> list[str]:
    bad = []
    for kind, found in (("读", reads), ("写", writes)):
        for key, values in sorted(found.items()):
            for value, where in sorted(values.items()):
                if value not in _allowed(doc, default, key):
                    bad.append(f"{kind} {key[0]}.{key[1]} = {value!r}  {sorted(where)[:2]}  注释={sorted(doc[key])}")
    return bad


def _odd_instance(doc: dict, default: dict, instance: dict) -> list[str]:
    vocabulary = set().union(*doc.values()) | set(default.values())
    return sorted(f".{attr} {value!r}  {sorted(where)[:2]}"
                  for attr, values in instance.items() for value, where in values.items() if value not in vocabulary)


def offenders() -> list[str]:
    """三条规矩的违例合在一起（闸门现状表取它的条数，判据上线即 0）。"""
    doc, default, reads, writes, instance = _all()
    return _bare(doc) + _bad_reads_writes(doc, default, reads, writes) + _odd_instance(doc, default, instance)


def test_字符串状态列都写明了取值():
    doc, _default, *_ = _all()
    bare = _bare(doc)
    assert bare == [], (
        "这些状态列上方没有「取值=中文, …」注释（CLAUDE.md §4：取值范围写在列注释里）：\n  " + "\n  ".join(bare)
    )
    assert len(doc) >= 80, f"只认出 {len(doc)} 个状态列——列声明的写法变了，判据在空转"


def test_按模型读写的取值都在这一列的注释里():
    doc, default, reads, writes, _instance = _all()
    bad = _bad_reads_writes(doc, default, reads, writes)
    assert bad == [], (
        "以下取值不在这一列的注释 / 默认值里——写岔了（比较永远为假、筛选永远是空），或新增了取值却没登记：\n  "
        + "\n  ".join(bad) + "\n是笔误就改代码；是新取值就补进列上方的注释。"
    )
    assert sum(len(v) for v in reads.values()) >= 100 and sum(len(v) for v in writes.values()) >= 30, (
        "认出的读写处太少——判据在空转（比较 / update / 构造 / move_row 的写法变了？）"
    )


def test_实例上的状态字面量至少在某一列的注释里出现过():
    doc, default, _reads, _writes, instance = _all()
    odd = _odd_instance(doc, default, instance)
    assert odd == [], "以下状态字面量在任何一列的注释里都没出现过——多半是写岔了：\n  " + "\n  ".join(odd)
    assert sum(len(v) for v in instance.values()) >= 60, "实例层认出的太少——判据在空转"


def test_判据自证():
    """注释写明的认、默认值认、常量元组展开；写岔的读、没登记的写、实例上的怪值都报；没注释的列报。"""
    models = {"m.py": (
        "class Job(Base):\n"
        "    # queued=排队, done=完成\n"
        "    status: Mapped[str] = mapped_column(String(16), default=\"queued\")\n"
        "    other_status: Mapped[str] = mapped_column(String(16), default=\"\")\n"
    )}
    doc, default = documented(models)
    assert doc == {("Job", "status"): {"queued", "done"}, ("Job", "other_status"): set()}
    assert default == {("Job", "status"): "queued", ("Job", "other_status"): ""}
    code = {str(SERVER / "app" / "x.py"): (
        "OPEN = (\"queued\", \"runing\")\n"
        "def f(db, job):\n"
        "    db.query(Job).filter(Job.status.in_(OPEN))\n"
        "    db.query(Job).filter(Job.status == \"done\").update({Job.status: \"finished\"})\n"
        "    Job(status=\"queued\")\n"
        "    job.status = \"doen\"\n"
    )}
    reads, writes, instance = usages(code, doc)
    assert set(reads[("Job", "status")]) == {"queued", "runing", "done"}
    assert set(writes[("Job", "status")]) == {"finished", "queued"}
    assert set(instance["status"]) == {"doen"}
    assert {v for v in reads[("Job", "status")] if v not in _allowed(doc, default, ("Job", "status"))} == {"runing"}
