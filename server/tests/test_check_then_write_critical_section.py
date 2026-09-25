"""「先查别的行判定、再写」的判定与写入必须圈在同一个临界区里（P1-116 / P1-117 的防复发闸门）。

## 缺陷形状

判定读的是**别的行**——区间有没有重叠（手术间时段）、同组的比例加起来超没超 100（成本分摊）、
已有几条（家庭代管上限）——然后插一条或改一条。唯一约束管不到这种不变式：它只认「键相同」，
不认「区间相交」「合计超限」。INSERT 又不给任何既有行加锁，PG 的 READ COMMITTED 下并发事务
同时读到「没重叠 / 没超」、各写一条。开发库 SQLite 的库级写锁把窗口一并锁掉，所以只在生产库现形：

- P1-117：真 PG 八路同时排同一手术间起点错开的重叠时段，**八台全排进去**（修前代码 3 次 3 次如此）；
- P1-116：同一来源科室八路并发各建一条 60% 的分摊规则，5～7 路成功、合计 300%～420%。

修法是 `concurrency.serialized_on`（PG 上对「界」那一行 `SELECT … FOR UPDATE`，SQLite 上进程内锁），
判定与写入（含 commit）都在块里。

## 判据（派生、零基线）

扫 `app/` 下全部模块级函数：

1. **判定点**：`if` 分支里 `raise`，条件（顺着函数内的赋值一路追名字）落到一次**读**取数上，且取数带
   「不等比较的过滤」（`Model.col < x` 一类，区间 / 上下限）或聚合（`func.sum/count/max/min`、`.count()`）。
   取数链里有 `.update(` / `.delete(` 的是条件写（`UPDATE … WHERE booked < capacity`），本身原子，不算；
2. **判定帮手**：自己含判定点、自己不写库的函数（如 `cost._check_ratio_budget`）；别处调它即视同判定点；
3. 函数在判定点之后写库（`add` / `add_all` / `insert_or_conflict` / `.update(` / `.values(` / `.delete(` /
   `commit`），则判定点**与其后至少一处写**必须在同一个 `with serialized_on(...)` 块里。

判据有盲区（跨模块的判定帮手、把判定结果先存进对象属性再判），按惯例不假装覆盖全了：本闸门盯的是
已经实测出事的两种写法，以后撞见新写法再补判据。
"""
from __future__ import annotations

import ast
import pathlib
import textwrap

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

INEQ = (ast.Lt, ast.Gt, ast.LtE, ast.GtE)
READ_CALLS = {"query", "execute", "scalar", "scalars"}
WRITE_SUFFIXES = (".add", ".add_all", ".update", ".values", ".delete", ".commit")
WRITE_NAMES = {"insert_or_conflict", "insert_if_absent"}

# 待复核（只减不增）：扫出来、判为同一缺陷形状，但所在链路按 CLAUDE.md §8 要人复核才能动的（TECH_DEBT P1-119）。
PENDING_REVIEW = {
    "routers/portal.py:add_family_member": (
        "居民端家庭代管「最多 N 位」先数后插：同一账户并发添加可越过上限（每一路仍要各自过第二因子）。"
        "代管授予属认证 / 越权链路（与 P1-114 未改的那一半同一处）。"
    ),
    "routers/portal.py:_consume_code": (
        "短信验证码「查未消费 → 置已消费」：同一条码并发提交两次，两路都读到未消费、都算验过（一次性码用了两次）。"
        "修法是条件 UPDATE（`WHERE consumed = false` 看影响行数），但这是登录 / 绑定的验码链路。"
    ),
    "routers/users.py:set_user_status": (
        "「不可停用最后一个可用管理员」先数后改：仅剩的两名管理员同时互相停用，两路各数到对方之外还有一名，"
        "都放行——平台再无可用管理员。没有天然的「界」行可锁（要锁住全体在用管理员行再数），属账号管理链路。"
    ),
}

# 已判：并发穿过去也不出错，写明为什么（只减不增）。
ACCEPTED = {
    "routers/performance.py:update_indicator": (
        "「启用指标权重合计须大于 0」：两名院长同时把最后两个有权重的指标各停一个，可以合计为 0；但计分侧"
        "（`_normalized_weights`）本就只取启用且权重大于 0 的指标、一个没有就退回默认权重，不会除零也不会算错；"
        "且指标目录没有天然的「界」行可锁。"
    ),
}

# 已扫出、下一提交就修的（只减不增，修一条划一条）。
KNOWN_UNFIXED = {
    "spd/routers/tasks.py:advance_instance": (
        "手工推进路径：两路同时数到「当前节点没有未完成任务」，都从同一节点推进，下一节点的任务派两份（P1-118）。"
    ),
}

REGISTERED = {**PENDING_REVIEW, **ACCEPTED, **KNOWN_UNFIXED}


def _module_functions(tree: ast.Module):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _bindings(func) -> dict[str, list[ast.AST]]:
    out: dict[str, list[ast.AST]] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        out.setdefault(name.id, []).append(node.value)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            if isinstance(node.target, ast.Name):
                out.setdefault(node.target.id, []).append(node.value)
    return out


def _closure(expr: ast.AST, bindings) -> list[ast.AST]:
    """条件表达式本身 + 顺着名字追到的全部赋值右侧。"""
    seen: set[str] = set()
    exprs = [expr]
    stack = [expr]
    while stack:
        current = stack.pop()
        for node in ast.walk(current):
            if isinstance(node, ast.Name) and node.id not in seen and node.id in bindings:
                seen.add(node.id)
                for value in bindings[node.id]:
                    exprs.append(value)
                    stack.append(value)
    return exprs


def _is_model_column(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id[:1].isupper()


def _read_query_kinds(exprs) -> set[str]:
    """取数里的判定种类；取数链带 update/delete（条件写）的整条不算。"""
    kinds: set[str] = set()
    for expr in exprs:
        calls = [n for n in ast.walk(expr) if isinstance(n, ast.Call)]
        attrs = {c.func.attr for c in calls if isinstance(c.func, ast.Attribute)}
        is_read = bool(attrs & READ_CALLS) or any(isinstance(c.func, ast.Name) and c.func.id == "select" for c in calls)
        if not is_read or attrs & {"update", "delete"}:
            continue
        for call in calls:
            if not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr in ("filter", "where"):
                for arg in call.args:
                    for cmp in ast.walk(arg):
                        if (isinstance(cmp, ast.Compare) and any(isinstance(op, INEQ) for op in cmp.ops)
                                and any(_is_model_column(side) for side in [cmp.left, *cmp.comparators])):
                            kinds.add("range")
            elif call.func.attr == "count" and not call.args:
                kinds.add("count")
            elif (isinstance(call.func.value, ast.Name) and call.func.value.id == "func"
                  and call.func.attr in ("sum", "count", "max", "min")):
                kinds.add("aggregate")
    return kinds


def _raises(stmts) -> bool:
    return any(isinstance(n, ast.Raise) for stmt in stmts for n in ast.walk(stmt))


def _guard_sites(func) -> list[ast.AST]:
    bindings = _bindings(func)
    return [
        node for node in ast.walk(func)
        if isinstance(node, ast.If) and _raises(node.body) and _read_query_kinds(_closure(node.test, bindings))
    ]


def _writes(func) -> list[ast.Call]:
    out = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        if name.endswith(WRITE_SUFFIXES) or name in WRITE_NAMES or name.split(".")[-1] in WRITE_NAMES:
            out.append(node)
    return out


def _serialized_blocks(func) -> list[set[int]]:
    blocks = []
    for node in ast.walk(func):
        if isinstance(node, (ast.With, ast.AsyncWith)) and any(
            "serialized_on" in ast.unparse(item.context_expr) for item in node.items
        ):
            blocks.append({id(sub) for sub in ast.walk(node)})
    return blocks


def offenders_in(source: str, label: str) -> dict[str, str]:
    """`{文件:函数 → 判定点那一行}`：判定之后写库、判定与其后的写不在同一个 serialized_on 块里的。"""
    tree = ast.parse(source)
    funcs = list(_module_functions(tree))
    checkers = {f.name for f in funcs if _guard_sites(f) and not _writes(f)}
    out: dict[str, str] = {}
    for func in funcs:
        sites = _guard_sites(func) + [
            node for node in ast.walk(func)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in checkers
        ]
        writes = _writes(func)
        if not writes:
            continue
        blocks = _serialized_blocks(func)
        for site in sites:
            later = [w for w in writes if w.lineno > site.lineno]
            if not later:
                continue
            if any(id(site) in block and any(id(w) in block for w in later) for block in blocks):
                continue
            out[f"{label}:{func.name}"] = f"第 {site.lineno} 行：{ast.unparse(site).splitlines()[0][:80]}"
            break
    return out


def all_offenders() -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(APP_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        out.update(offenders_in(path.read_text(encoding="utf-8"), path.relative_to(APP_DIR).as_posix()))
    return out


def test_先查别的行再写_判定与写入在同一临界区里():
    unregistered = {k: v for k, v in all_offenders().items() if k not in REGISTERED}
    assert not unregistered, (
        "判定读的是别的行（区间重叠 / 合计超限 / 条数上限）、随后写库，却不在同一个 serialized_on 块里——"
        "PG 上并发请求会同时读到「没超」、各写一条（P1-116 / P1-117 实测）。把判定与写入（含 commit）圈进"
        f"以「界」那一行为锁的 `with serialized_on(db, Model, id):`：\n{unregistered}"
    )


def test_登记名单不得腐烂():
    stale = set(REGISTERED) - set(all_offenders())
    assert not stale, f"这些已不再命中判据（修掉了或挪了位置），从登记名单里划掉：{stale}"
    assert not set(PENDING_REVIEW) & set(ACCEPTED) and not set(KNOWN_UNFIXED) & (set(PENDING_REVIEW) | set(ACCEPTED))


def test_修过的三处被判据看见且判为已圈住():
    """覆盖面自证：判据在真实代码上认得出这三处是「先查别的行再写」，且判它们已圈进临界区。

    认不出来（重构成判据看不见的写法）闸门就成了摆设——这条先红。"""
    for rel, names in {
        "routers/surgery.py": {"schedule_surgery"},
        "routers/cost.py": {"create_allocation_rule", "update_allocation_ratio"},
    }.items():
        source = (APP_DIR / rel).read_text(encoding="utf-8")
        tree = ast.parse(source)
        funcs = {f.name: f for f in _module_functions(tree)}
        checkers = {f.name for f in funcs.values() if _guard_sites(f) and not _writes(f)}
        for name in names:
            func = funcs[name]
            calls_checker = any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in checkers
                for n in ast.walk(func)
            )
            assert _guard_sites(func) or calls_checker, f"{rel}:{name} 判据认不出判定点"
            assert _serialized_blocks(func), f"{rel}:{name} 没有 serialized_on 块"
        assert not any(k.split(":")[1] in names for k in offenders_in(source, rel)), rel


def _probe(body: str) -> dict[str, str]:
    return offenders_in(textwrap.dedent(body), "probe.py")


def test_判据自证():
    """规则自己的行为用合成代码钉住：该红的红、该放的放。"""
    overlap_unguarded = """
        def book(db, body):
            hit = db.query(Slot).filter(Slot.room_id == body.room_id, Slot.start < body.end).first()
            if hit is not None:
                raise HTTPException(409, "占用")
            db.add(Slot(**body))
            db.commit()
    """
    assert "probe.py:book" in _probe(overlap_unguarded)

    overlap_guarded = """
        def book(db, body):
            with serialized_on(db, Room, body.room_id):
                hit = db.query(Slot).filter(Slot.room_id == body.room_id, Slot.start < body.end).first()
                if hit is not None:
                    raise HTTPException(409, "占用")
                db.add(Slot(**body))
                db.commit()
    """
    assert _probe(overlap_guarded) == {}

    # 判定在块里、写在块外：锁在判定之后就放了，窗口照旧
    write_outside = """
        def book(db, body):
            with serialized_on(db, Room, body.room_id):
                hit = db.query(Slot).filter(Slot.start < body.end).first()
                if hit is not None:
                    raise HTTPException(409, "占用")
            db.add(Slot(**body))
            db.commit()
    """
    assert "probe.py:book" in _probe(write_outside)

    # 聚合经名字转手、再经判定帮手调用（P1-116 的原形）
    helper_unguarded = """
        def _budget(db, dept_id, pct):
            query = db.query(func.sum(Rule.pct)).filter(Rule.dept_id == dept_id)
            used = float(query.scalar() or 0)
            if used + pct > 100:
                raise HTTPException(422, "超了")

        def create(db, body):
            _budget(db, body.dept_id, body.pct)
            db.add(Rule(**body))
            db.commit()
    """
    assert set(_probe(helper_unguarded)) == {"probe.py:create"}

    # 条数上限
    count_limit = """
        def add_member(db, account):
            if db.query(Member).filter(Member.account_id == account.id).count() >= 5:
                raise HTTPException(409, "满了")
            db.add(Member(account_id=account.id))
            db.commit()
    """
    assert "probe.py:add_member" in _probe(count_limit)

    # 条件写本身原子（UPDATE … WHERE booked < capacity），不算判定点
    atomic_claim = """
        def claim(db, slot_id):
            claimed = db.query(Slot).filter(Slot.id == slot_id, Slot.booked < Slot.capacity).update(
                {Slot.booked: Slot.booked + 1})
            if not claimed:
                raise HTTPException(409, "约满")
            db.add(Appointment(slot_id=slot_id))
            db.commit()
    """
    assert _probe(atomic_claim) == {}

    # 等值查重不在本闸门范围（唯一约束 + insert_or_conflict 那条闸门管）
    equality_only = """
        def create(db, body):
            if db.query(Rule).filter(Rule.code == body.code).first():
                raise HTTPException(409, "重复")
            db.add(Rule(**body))
            db.commit()
    """
    assert _probe(equality_only) == {}
