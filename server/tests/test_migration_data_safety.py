"""迁移数据安全闸门：迁移不得静默改动存量业务数据。

**为什么要这道闸门。** `e5b7c9d1f3a4`（住院结算单唯一索引）遇到存量重复时选择了
不阻塞路径——跳过建索引、打一条指名 `admission_id` 的 ERROR 日志、把补建 SQL 写进
docstring，理由是"财务凭证不能由程序替人决定删哪张"。这条判断是对的，但它在平台上
并没有被一致执行：`d3e4f5a6b7c8` 为了给 `resident_accounts.patient_id` 建部分唯一
索引，直接 `UPDATE ... SET patient_id = NULL`，把"一档多户"里除 id 最小以外的账户
**静默解绑**；受影响居民下次登录发现自己看不到档案，而库里没有任何记录说明是谁在
什么时候被解绑的——信息已经丢了，事后连"谁被解绑过"都查不回来。`d5e6f7a8b9c0` 是
同一个形状的轻症版（把重复的全域基金池改成 closed）。

判断一致不一致不能靠人记，所以落成扫描：把"破坏性数据变更"的形状写死，新迁移一旦
长成那个样子就变红，除非进显式豁免清单（带书面理由，只减不增）。

**三档分类**（与 CLAUDE.md §4 的约定同源）：

* **A 破坏性**——删除/置空业务字段、丢失关联、改写不是本迁移新加的业务列。必须改成
  不阻塞路径（检测到冲突就跳过约束 + ERROR 日志指名冲突记录 + docstring 给人工
  处置 SQL），或直接失败并给出处置 SQL。**闸门对 A 档判红。**
* **B 补值性**——给本迁移新加的列填默认值/派生值，或把既有列的空值补上（WHERE 该列
  IS NULL / = ''，天然幂等）。可接受，但要能复算、能修。
* **C 结构性**——纯 DDL，不动任何存量行。

**判定是 fail-closed 的**：SET 目标列解析不出来（f-string 拼列名等）一律按 A 处理，
要么改成能静态看懂的写法，要么进豁免清单写明理由。宁可误报也不漏报——漏报的代价是
下一次静默解绑。

启发式的边界写在 `_split_set_clause` / `_targets_of` 的注释里：SQL 用正则解析而不是
真 parser，因为迁移里的 SQL 都是本仓库自己写的字面量，形状可控；真 parser 的收益
覆盖不了它的依赖成本（CLAUDE.md §1 第 12 条）。
"""
from __future__ import annotations

import ast
import json
import pathlib
import re
import warnings
from dataclasses import dataclass, field

VERSIONS = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions"


# ===========================================================================
# 豁免清单：**只减不增**。每条必须写明"为什么这次静默改数据是可接受的"。
# 加新条目 = 承认新迁移在静默改存量数据，请先改迁移，改不动再来改这里。
# ===========================================================================
_EXEMPTIONS: dict[str, str] = {
    "a4b5c6d7e8f9": (
        "PII 检索索引回填：SET 目标列是 f-string 拼出来的（{idx_col}），静态解析不出来，"
        "按 fail-closed 落到 A 档。实际是 B 档标准范式——三个 *_idx 列都由本迁移新加，"
        "WHERE 带 `idx IS NULL` 天然幂等，写错了有 scripts/pii_encrypt_backfill.py "
        "--rebuild-index 可复算重建，且真要写索引前先校验密钥非默认值（_assert_real_secret）。"
        "不动任何存量业务列。"
    ),
    "f7a8b9c0d1e2": (
        "appointment_blacklist → service_blacklists 的表泛化搬迁：upgrade 里的 "
        "drop_table 前面就是整表 INSERT ... SELECT，行一条不少地搬到了新表，属结构性"
        "搬迁而非数据销毁。**已知缺口**：downgrade 反向搬迁时只搬 domain='appointment' "
        "的行，其余 domain 的行会随 service_blacklists 一起消失——回退前须先导出 "
        "`SELECT * FROM service_blacklists WHERE domain <> 'appointment'`，见 "
        "docs/运维手册.md。"
    ),
}


# ===========================================================================
# 扫描器
# ===========================================================================
_KIND_DESTRUCTIVE = {
    "置空业务列",
    "删除业务行",
    "改写既有业务列",
    "SET目标列解析不出",
    "upgrade里删表删列",
}


@dataclass
class Finding:
    func: str          # upgrade / downgrade
    lineno: int
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.func}():{self.lineno} [{self.kind}] {self.detail}"


@dataclass
class Scan:
    path: pathlib.Path
    revision: str
    findings: list[Finding] = field(default_factory=list)
    stmts: int = 0     # 识别出的数据变更语句条数

    @property
    def destructive(self) -> list[Finding]:
        return [f for f in self.findings if f.kind in _KIND_DESTRUCTIVE]

    @property
    def tier(self) -> str:
        if self.destructive:
            return "A"
        return "B" if self.findings else "C"


def _split_set_clause(sql: str) -> str:
    """取 UPDATE 的 SET 子句（到括号深度 0 的 WHERE 为止）。

    深度感知是为了不被子查询里的 WHERE 截断，例如
    `SET n = (SELECT COUNT(*) FROM t WHERE ...)` 的那个 WHERE 不算数。
    """
    m = re.search(r"\bSET\b", sql, re.I)
    if not m:
        return ""
    rest = sql[m.end():]
    depth = 0
    for i, ch in enumerate(rest):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and rest[i:i + 5].upper() == "WHERE":
            if i == 0 or not (rest[i - 1].isalnum() or rest[i - 1] == "_"):
                return rest[:i]
    return rest


def _where_clause(sql: str) -> str:
    """SET 子句之后的部分（含 WHERE）——用于判断是不是"只补空值"。"""
    set_clause = _split_set_clause(sql)
    if not set_clause:
        return sql
    idx = sql.find(set_clause)
    return sql[idx + len(set_clause):] if idx >= 0 else sql


def _targets_of(set_clause: str) -> tuple[list[str], bool]:
    """SET 子句里的目标列名，以及"是否全部解析成功"。

    只认 `SET col =` 与顶层 `, col =` 两种位置；括号内的 `a = b`（子查询条件）
    不算目标。解析不出目标（f-string 拼列名、动态表达式）时第二个返回值为 False，
    调用方按 fail-closed 判 A 档。
    """
    cols: list[str] = []
    depth = 0
    token = ""
    expect_target = True   # SET 之后 / 顶层逗号之后，下一个标识符是目标列
    for ch in set_clause:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and ch == ",":
            expect_target = True
            token = ""
            continue
        elif depth == 0 and ch == "=" and expect_target:
            name = token.strip().split(".")[-1]
            if re.fullmatch(r"[A-Za-z_]\w*", name):
                cols.append(name)
            else:
                return cols, False     # 解析不出来 → fail-closed
            expect_target = False
            token = ""
            continue
        if depth == 0 and expect_target:
            token += ch
    return cols, bool(cols)


def _column_names(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "Column"
            and sub.args
            and isinstance(sub.args[0], ast.Constant)
            and isinstance(sub.args[0].value, str)
        ):
            out.add(sub.args[0].value)
    return out


def _collect_added_columns(tree: ast.Module) -> set[tuple[str, str]]:
    """本迁移新加的 (表, 列)。

    三种写法都认：`op.add_column("t", sa.Column("c"))`、`op.create_table("t", ...)`、
    以及 `with op.batch_alter_table("t") as b: b.add_column(sa.Column("c"))`——
    最后一种的表名在 with 语句上，所以单独走一遍 With 节点。
    **带表名**是必要的精度：`d5e6f7a8b9c0` 既给 vaccine_contraindications 新加了
    `status` 列，又在改 fund_pools 的 `status`，只按列名匹配会把后者漏判成回填。
    """
    pairs: set[tuple[str, str]] = set()

    def _table_arg(call: ast.Call) -> str:
        if call.args and isinstance(call.args[0], ast.Constant):
            return str(call.args[0].value)
        return ""

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("add_column", "create_table"):
                table = _table_arg(node)
                if table:
                    for arg in node.args[1:]:
                        for col in _column_names(arg):
                            pairs.add((table, col))
        if isinstance(node, ast.With):
            for item in node.items:
                call = item.context_expr
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "batch_alter_table"
                ):
                    continue
                table = _table_arg(call)
                if not table:
                    continue
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "add_column"
                    ):
                        for arg in sub.args:
                            for col in _column_names(arg):
                                pairs.add((table, col))
    return pairs


def _sql_literals(fn: ast.FunctionDef) -> list[tuple[int, str]]:
    """函数体里长得像 SQL 的字符串（含 f-string，按源码文本取）。

    相邻字符串字面量拼接由 ast 自动合并；f-string 用 ast.unparse 还原成源码文本，
    占位符原样保留（`{idx_col}` 之类正是要触发 fail-closed 的那一类）。
    """
    out: list[tuple[int, str]] = []
    # f-string 的字面量碎片（'UPDATE ' / ' SET ' …）自己也是 Constant 节点，
    # 单独看会被当成半条 SQL 重复计一次——整条 JoinedStr 已经覆盖它们了。
    inner = {
        id(sub)
        for node in ast.walk(fn) if isinstance(node, ast.JoinedStr)
        for sub in ast.walk(node) if isinstance(sub, ast.Constant)
    }
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in inner:
                continue
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = ast.unparse(node)
        else:
            continue
        if re.search(r"\b(UPDATE|DELETE\s+FROM|INSERT\s+INTO)\b", text, re.I):
            out.append((node.lineno, " ".join(text.split())))
    return out


def _expression_updates(fn: ast.FunctionDef) -> list[tuple[int, str, list[str], bool, str]]:
    """SQLAlchemy 表达式写法的 UPDATE：`tbl.update().values(col=...)`。

    返回 (行号, 表变量名, 目标列, 是否有 None 值, 整条表达式源码)。
    """
    out: list[tuple[int, str, list[str], bool, str]] = []
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "values":
            continue
        chain = ast.unparse(node)
        if ".update()" not in chain:
            continue
        var = chain.split(".update()")[0].strip()
        cols = [kw.arg for kw in node.keywords if kw.arg]
        has_none = any(
            isinstance(kw.value, ast.Constant) and kw.value.value is None for kw in node.keywords
        )
        out.append((node.lineno, var, cols, has_none, chain))
    return out


def _sa_table_vars(tree: ast.Module) -> dict[str, str]:
    """`users = sa.table("users", ...)` 这类别名 → 真表名的映射。

    表达式写法的 UPDATE 目标表藏在变量里，不解开就没法做"表+列"精度的判定。
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        call = node.value
        if not (isinstance(target, ast.Name) and isinstance(call, ast.Call)):
            continue
        func = call.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "table" and call.args and isinstance(call.args[0], ast.Constant):
            out[target.id] = str(call.args[0].value)
    return out


def _reachable_from(tree: ast.Module, entry: str) -> set[str]:
    """从 upgrade / downgrade 出发能调到的本模块函数名（含自身）。

    迁移常把回填抽成模块级 helper（`a4b5c6d7e8f9._backfill_idx` 就是），只扫
    upgrade/downgrade 的函数体会整段漏掉——那正是最该被扫的那段。
    """
    defined = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    if entry not in defined:
        return set()
    seen: set[str] = set()
    stack = [entry]
    while stack:
        cur = stack.pop()
        if cur in seen or cur not in defined:
            continue
        seen.add(cur)
        for node in ast.walk(defined[cur]):
            if isinstance(node, ast.Call):
                fname = node.func.id if isinstance(node.func, ast.Name) else ""
                if fname in defined and fname not in seen:
                    stack.append(fname)
    return seen


def _expression_deletes(fn: ast.FunctionDef) -> list[int]:
    """SQLAlchemy 表达式写法的 DELETE：`tbl.delete()`。"""
    return [
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "delete"
        and not node.args
    ]


def _blank_fill(where: str, col: str) -> bool:
    """WHERE 是否把目标列限定在"空值"上——是的话属补空值（幂等、可复算）。"""
    pat = rf"{re.escape(col)}\s*(?:IS\s+NULL|=\s*''|==\s*''|==\s*None)|{re.escape(col)}\.is_\(None\)"
    return bool(re.search(pat, where, re.I))


def _created_tables(tree: ast.Module, func: str) -> set[str]:
    out: set[str] = set()
    for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == func]:
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_table"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                out.add(str(node.args[0].value))
    return out


def _dropped_objects(fn: ast.FunctionDef) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("drop_table", "drop_column")
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            out.append((node.lineno, node.func.attr, str(node.args[0].value)))
    return out


def _revision_of(tree: ast.Module) -> str:
    for node in tree.body:
        targets: list[ast.expr]
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for t in targets:
            if (
                isinstance(t, ast.Name)
                and t.id == "revision"
                and isinstance(node.value, ast.Constant)
            ):
                return str(node.value.value)
    return ""


def _update_table(sql: str) -> str:
    m = re.search(r"\bUPDATE\s+([A-Za-z_]\w*)", sql, re.I)
    return m.group(1) if m else ""


def scan(path: pathlib.Path) -> Scan:
    """扫一个迁移文件，产出分档与逐条 finding。

    扫描面 = upgrade / downgrade **及它们能调到的本模块 helper**；两条入口都够不着
    的死代码不扫（跑不到就伤不到）。同一个 helper 被两边调到时，findings 记在
    upgrade 名下（更严的一侧）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    res = Scan(path=path, revision=_revision_of(tree))
    added = _collect_added_columns(tree)
    added_cols = {c for _, c in added}
    created = _created_tables(tree, "upgrade")
    tbl_vars = _sa_table_vars(tree)
    defined = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    up_reach = _reachable_from(tree, "upgrade")
    down_reach = _reachable_from(tree, "downgrade")
    entries: list[tuple[str, ast.FunctionDef]] = []
    for name in sorted(up_reach | down_reach):
        entries.append(("upgrade" if name in up_reach else "downgrade", defined[name]))

    def _is_new(table: str, col: str) -> bool:
        """这一列是不是本迁移刚加的（表名解析不出时退回按列名匹配）。"""
        if table:
            return (table, col) in added
        return col in added_cols

    for owner, fn in entries:
        for lineno, sql in _sql_literals(fn):
            if re.search(r"\bINSERT\s+INTO\b", sql, re.I):
                res.stmts += 1
                res.findings.append(Finding(owner, lineno, "插入行", sql[:110]))
                continue
            if re.search(r"\bDELETE\s+FROM\b", sql, re.I):
                res.stmts += 1
                res.findings.append(Finding(owner, lineno, "删除业务行", sql[:110]))
                continue
            if not re.search(r"\bUPDATE\b", sql, re.I):
                continue
            res.stmts += 1
            table = _update_table(sql)
            set_clause = _split_set_clause(sql)
            where = _where_clause(sql)
            if re.search(r"=\s*NULL\b", set_clause, re.I):
                res.findings.append(Finding(owner, lineno, "置空业务列", sql[:110]))
                continue
            cols, ok = _targets_of(set_clause)
            if not ok:
                res.findings.append(
                    Finding(owner, lineno, "SET目标列解析不出", f"SET {set_clause.strip()[:80]}")
                )
                continue
            existing = [c for c in cols if not _is_new(table, c) and not _blank_fill(where, c)]
            if existing:
                res.findings.append(
                    Finding(owner, lineno, "改写既有业务列", f"{table}.{existing}：{sql[:80]}")
                )
            else:
                kind = "回填新列" if any(_is_new(table, c) for c in cols) else "补空值"
                res.findings.append(Finding(owner, lineno, kind, f"{table}.{cols}"))

        for lineno, var, cols, has_none, chain in _expression_updates(fn):
            res.stmts += 1
            table = tbl_vars.get(var, "")
            if has_none:
                res.findings.append(Finding(owner, lineno, "置空业务列", f"values({cols})=None"))
                continue
            if not cols:
                res.findings.append(Finding(owner, lineno, "SET目标列解析不出", chain[:90]))
                continue
            existing = [c for c in cols if not _is_new(table, c) and not _blank_fill(chain, c)]
            if existing:
                res.findings.append(
                    Finding(owner, lineno, "改写既有业务列", f"{table}.{existing}")
                )
            else:
                kind = "回填新列" if any(_is_new(table, c) for c in cols) else "补空值"
                res.findings.append(Finding(owner, lineno, kind, f"{table}.{cols}"))

        for lineno in _expression_deletes(fn):
            res.stmts += 1
            res.findings.append(Finding(owner, lineno, "删除业务行", "表达式写法 .delete()"))

        if owner == "upgrade":
            for lineno, op_name, target in _dropped_objects(fn):
                if op_name == "drop_table" and target in created:
                    continue      # 本迁移自己建的中转表，删掉不丢存量
                res.stmts += 1
                res.findings.append(
                    Finding(owner, lineno, "upgrade里删表删列", f"{op_name}({target})：直接销毁存量")
                )
    return res


def _scan_all() -> list[Scan]:
    return [scan(p) for p in sorted(VERSIONS.glob("*.py")) if p.name != "__init__.py"]


# ===========================================================================
# 闸门
# ===========================================================================
def test_迁移不得静默改动存量业务数据():
    """A 档形状即变红——除非在 _EXEMPTIONS 里带书面理由。

    对应 CLAUDE.md §4"迁移不得静默改动存量业务数据"。整改范式见
    `e5b7c9d1f3a4` / `d3e4f5a6b7c8` / `d5e6f7a8b9c0`：检测到冲突不改数据，
    跳过约束 + ERROR 日志指名冲突记录 + docstring 里给人工处置 SQL。
    """
    offenders = {
        s.revision: [str(f) for f in s.destructive]
        for s in _scan_all()
        if s.destructive and s.revision not in _EXEMPTIONS
    }
    assert not offenders, (
        "以下迁移在静默改动存量业务数据（CLAUDE.md §4）：\n"
        + json.dumps(offenders, ensure_ascii=False, indent=2)
        + "\n\n改法（照 e5b7c9d1f3a4 的范式）：检测到冲突时**不要**替人删/改数据，"
        "改成①跳过该约束的建立 + 一条指名冲突记录（主键/业务键）的 ERROR 日志，"
        "或②直接失败；两种都要在 docstring 里写清人工处置 SQL。"
        "确有不可避免的例外，进 _EXEMPTIONS 并写明理由（该清单只减不增）。"
    )


def test_downgrade同样不得静默改动存量业务数据():
    """回退时丢数据同样致命——闸门对 downgrade 用同一套判定。

    单列出来是为了让失败信息直指"回退路径"：downgrade 平时没人跑，真跑的时候
    多半是生产出事的深夜，那时才发现丢了数据已经晚了。
    """
    bad = {}
    for s in _scan_all():
        if s.revision in _EXEMPTIONS:
            continue
        hits = [str(f) for f in s.destructive if f.func == "downgrade"]
        if hits:
            bad[s.revision] = hits
    assert not bad, (
        "以下迁移的 downgrade 会静默改动/销毁存量业务数据：\n"
        + json.dumps(bad, ensure_ascii=False, indent=2)
    )


def test_豁免清单只减不增且每条都有书面理由():
    """棘轮：豁免数量锁死在当前值，理由不能是占位符，失效的豁免必须删。"""
    assert len(_EXEMPTIONS) <= 2, (
        f"迁移数据安全豁免变多了（{len(_EXEMPTIONS)} > 2）。豁免只减不增："
        "新迁移请改成不阻塞路径，而不是加豁免。"
    )
    for rev, reason in _EXEMPTIONS.items():
        assert len(reason) >= 40, f"豁免 {rev} 的理由太短，说不清'为什么这次可以'"
    scans = _scan_all()
    known = {s.revision for s in scans}
    assert not set(_EXEMPTIONS) - known, (
        f"豁免清单里有已不存在的 revision（该删了）：{sorted(set(_EXEMPTIONS) - known)}"
    )
    # 豁免不是免检：被豁免的迁移必须确实还在触发 A 档形状，否则说明它已经整改完了
    still = {s.revision for s in scans if s.destructive}
    assert not set(_EXEMPTIONS) - still, (
        f"这些豁免已经不需要了（迁移已不含 A 档形状），请删除：{sorted(set(_EXEMPTIONS) - still)}"
    )


def test_闸门自证覆盖面():
    """闸门必须说得出自己扫了多少、抓到多少——否则"全绿"没有意义。"""
    scans = _scan_all()
    tiers: dict[str, list[str]] = {"A": [], "B": [], "C": []}
    for s in scans:
        tiers[s.tier].append(s.revision)
    kinds: dict[str, int] = {}
    for s in scans:
        for f in s.findings:
            kinds[f.kind] = kinds.get(f.kind, 0) + 1

    summary = "\n".join([
        "【迁移数据安全闸门 · 覆盖面】",
        f"  扫描迁移文件：{len(scans)} 个（{VERSIONS}）",
        f"  识别出数据变更语句：{sum(s.stmts for s in scans)} 条",
        f"  分档：A 破坏性 {len(tiers['A'])} 个 / B 补值性 {len(tiers['B'])} 个 / "
        f"C 结构性 {len(tiers['C'])} 个",
        f"  A 档明细：{sorted(tiers['A']) or '无'}",
        f"  B 档明细：{sorted(tiers['B'])}",
        f"  形状计数：{json.dumps(kinds, ensure_ascii=False)}",
        f"  豁免：{len(_EXEMPTIONS)} 条 —— {sorted(_EXEMPTIONS)}",
        "  downgrade 侧数据变更语句："
        f"{sum(1 for s in scans for f in s.findings if f.func == 'downgrade')} 条",
    ])
    print(summary)
    # print 在 `-q` 下会被吞掉；warning 会进 pytest 的 "warnings summary"，
    # 让覆盖面数字在 CI 默认输出里也看得见（自证覆盖面不能只在 -s 时才成立）。
    warnings.warn(summary, UserWarning, stacklevel=2)

    # 三条"扫描器还活着"的自检：文件数、语句数、以及两只已知形状的金丝雀。
    # 没有金丝雀的话，扫描器哪天正则写崩、全库判成 C 档，闸门照样全绿。
    assert len(scans) >= 85, f"迁移文件数看起来不对（{len(scans)}），扫描目录可能取错了"
    assert sum(s.stmts for s in scans) >= 8, "识别出的数据变更语句太少，扫描器多半失灵了"
    assert tiers["C"], "所有迁移都被判成有数据变更，扫描器多半失灵了"
    canary = {s.revision: s.tier for s in scans}
    assert canary.get("e6f7a8b9c0d1") == "B", (
        "金丝雀失灵：e6f7a8b9c0d1 的 medical_wastes.trace_code 逐行回填应判 B 档"
    )
    assert canary.get("f7a8b9c0d1e2") == "A", (
        "金丝雀失灵：f7a8b9c0d1e2 的 upgrade 里 drop_table 应判 A 档（已豁免，但形状要认得出）"
    )


# ===========================================================================
# 扫描器自检：闸门自己也要被测，否则"全绿"可能只是它瞎了
# ===========================================================================
_PROBE_HEADER = (
    'revision = "probe0000"\n'
    "down_revision = None\n"
    "import sqlalchemy as sa\n"
    "from alembic import op\n\n\n"
)


def _classify(tmp_path, body: str) -> Scan:
    f = tmp_path / "probe_migration.py"
    f.write_text(_PROBE_HEADER + body, encoding="utf-8")
    return scan(f)


def test_扫描器认得出A档的四种形状(tmp_path):
    """每种 A 档形状造一份最小迁移，确认扫描器判红。

    这是把"造个临时迁移看闸门变不变红"的一次性验证固化下来：闸门的正则/AST
    以后被谁改坏了，这几条会当场变红，而不是等下一次静默解绑上线才发现。
    """
    cases = {
        "置空业务列":
            'def upgrade():\n'
            '    op.execute("UPDATE resident_accounts SET patient_id = NULL WHERE id > 0")\n',
        "删除业务行":
            'def upgrade():\n'
            '    op.execute("DELETE FROM settlements WHERE admission_id IS NULL")\n',
        "改写既有业务列":
            'def upgrade():\n'
            "    op.execute(\"UPDATE fund_pools SET status = 'closed' WHERE org_group_id IS NULL\")\n",
        "upgrade里删表删列":
            'def upgrade():\n'
            '    op.drop_column("patients", "phone")\n',
    }
    for kind, body in cases.items():
        res = _classify(tmp_path, body)
        assert res.tier == "A", f"{kind} 应判 A 档，实为 {res.tier}"
        assert any(f.kind == kind for f in res.findings), (
            f"{kind} 的形状没被认出来，findings={[str(f) for f in res.findings]}"
        )


def test_扫描器不误伤B档与C档(tmp_path):
    """回填新列 / 补空值 / 纯 DDL 不该被判红——误报多了就没人看闸门了。"""
    backfill = (
        "def upgrade():\n"
        '    op.add_column("training_plans", sa.Column("enrolled_count", sa.Integer()))\n'
        '    op.execute("UPDATE training_plans SET enrolled_count = 0")\n'
    )
    blank_fill = (
        "def upgrade():\n"
        "    op.execute(\"UPDATE exam_reports SET critical_status = 'notified' "
        'WHERE critical_status IS NULL")\n'
    )
    ddl_only = 'def upgrade():\n    op.create_index("ix_x", "patients", ["phone"])\n'
    assert _classify(tmp_path, backfill).tier == "B", "给本迁移新加的列回填应判 B 档"
    assert _classify(tmp_path, blank_fill).tier == "B", "只补既有列的空值应判 B 档"
    assert _classify(tmp_path, ddl_only).tier == "C", "纯 DDL 应判 C 档"


def test_扫描器扫得到helper里的数据变更(tmp_path):
    """回填抽成模块级 helper 也躲不掉——只扫 upgrade 函数体是最容易漏的那个洞。"""
    body = (
        "def _backfill():\n"
        '    op.execute("UPDATE resident_accounts SET patient_id = NULL")\n'
        "\n\n"
        "def upgrade():\n"
        "    _backfill()\n"
    )
    res = _classify(tmp_path, body)
    assert res.tier == "A", f"helper 里的置空没被扫到（实为 {res.tier} 档）"
    assert res.findings[0].func == "upgrade", "helper 的 finding 应记在调用它的 upgrade 名下"


def test_扫描器对解析不出的SET目标fail_closed(tmp_path):
    """列名拼不出来时宁可误报：看不懂的写法必须被人看一眼，不能默认放行。"""
    body = (
        "def upgrade():\n"
        '    col = "patient_id"\n'
        '    op.execute(f"UPDATE resident_accounts SET {col} = 1")\n'
    )
    assert _classify(tmp_path, body).tier == "A", "SET 目标解析不出应 fail-closed 判 A 档"
