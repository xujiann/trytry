"""时间口径闸门：取值的**时刻**和取值的**来源**，两件事各守一条。

## 这个文件为什么是新建的

`app/clock.py` 的模块 docstring 从一开始就写着：

    `test_clock.py` 有一条扫描用例：`app/` 下不得再出现裸 `datetime.utcnow()`
    或 `datetime.now()`，新代码一律走本模块。

全仓搜过——除了这句话本身，`test_clock.py` 从来不存在。P0-2 把四套时间函数收敛
成一个入口是真做了的，只有那道守住它的闸门没做。**一句声称守卫存在的注释**，
今天是第二次咬人了（上一次是 `spd/tasks.py:batch_tasks` 的注释说
「单条接口一直是校验的（见 claim_task）」，而 claim_task 恰恰没校验）。
注释写完就再没人回头核，而它读起来比没有注释更让人放心。

## 两条线

- **来源**（§1、§2）：时间从哪里取。`app/clock.py` 是声明中的唯一入口。
- **时刻**（§3）：时间在**什么时候**取。在模块顶层取，就是在 import 那一刻取；
  之后整轮测试（CI run 552 实测 62 分钟）里这个值都不再变，而服务端是在每次
  请求时才算。跨午夜的那一班 CI 必红——判据与被判对象取自两个时刻。
"""
import ast
import pathlib

import pytest

from conftest import (
    business_today,
    business_today_str,
    freeze_business_date,
    utc_today_str,
)

SERVER = pathlib.Path(__file__).resolve().parents[1]
APP = SERVER / "app"
TESTS = SERVER / "tests"

#: `app/clock.py` 自己当然要调底层时间函数——它就是那个入口。
CLOCK_SELF = "app/clock.py"

#: 点号调用：按**后缀**匹配，这样 `datetime.datetime.now()`（`import datetime`
#: 的模块写法）与 `from datetime import datetime` 的 `datetime.now()` 都认得。
#: 本仓目前只有后者，写成后缀匹配是为了不再栽在"判据只认一种写法"上。
#: 注意 `datetime.min.time()` 后缀是 `min.time`，不会被误判成 `time.time`。
DOTTED_CLOCKS = {
    "date.today": "日期",
    "datetime.now": "时刻",
    "datetime.utcnow": "时刻",
    "datetime.today": "时刻",
    "time.time": "时刻",
    "time.monotonic": "时刻",
}

#: 裸函数名调用：`from app.clock import now_naive` / `from conftest import ...`
#: 之后的写法。只在 §3（取值时刻）用——它们本身就是合规来源，§1/§2 不管。
BARE_CLOCKS = {
    "utcnow", "now_naive", "now_aware", "now_local", "today", "today_str",
    "business_today", "business_today_str", "utc_today_str",
}


def _alias_map(tree: ast.Module) -> dict[str, str]:
    """局部名 → 原名，好让改了名的 import 一样认得。

    `from datetime import date as d` 之后 `d.today()` 与 `date.today()` 完全等价，
    判据却只认后者——这是本仓今天第五次栽在"只认一种写法"上，所以这次先解别名
    再匹配。变异实测：不解别名时，注入 `from datetime import date as _d` +
    `_d.today()` 两道闸门都照样报绿。

    不覆盖动态取属性（`getattr(datetime, "now")()`）——那不是人会写的形状，
    真写了也躲不过 review；这里只保证等价的**静态**写法不成为后门。
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    out[alias.asname] = alias.name        # import datetime as dt
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                out[alias.asname or alias.name] = alias.name   # from datetime import date as d
    return out


def _dotted(node: ast.expr, aliases: dict[str, str]) -> str | None:
    """把 `a.b.c` 形状的属性链摊平成 "a.b.c"（根名先过一遍别名表）。"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(aliases.get(node.id, node.id))
    return ".".join(reversed(parts))


def _dotted_clock(call: ast.AST, aliases: dict[str, str] = {}) -> str | None:
    if not isinstance(call, ast.Call):
        return None
    name = _dotted(call.func, aliases)
    if name is None:
        return None
    for suffix in DOTTED_CLOCKS:
        if name == suffix or name.endswith("." + suffix):
            return suffix
    return None


def _any_clock(call: ast.AST, aliases: dict[str, str] = {}) -> str | None:
    """§3 用：点号写法与裸名字写法都算一次"取时间"。"""
    hit = _dotted_clock(call, aliases)
    if hit:
        return hit
    if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
        if aliases.get(call.func.id, call.func.id) in BARE_CLOCKS:
            return call.func.id + "()"
    return None


def _py_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(root.rglob("*.py"))


def _rel(path: pathlib.Path) -> str:
    return path.relative_to(SERVER).as_posix()


def _scan_dotted(root: pathlib.Path, suffix: str, *, skip: set[str] = frozenset()):
    """全文件范围（含函数体）找某一种点号时间调用。"""
    out = []
    for path in _py_files(root):
        rel = _rel(path)
        if rel in skip:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _alias_map(tree)
        for node in ast.walk(tree):
            if _dotted_clock(node, aliases) == suffix:
                out.append(f"{rel}:{node.lineno}")
    return out


# ---------------------------------------------------------------- §1 来源


def test_app_内不得再出现裸的_datetime_now_或_utcnow():
    """`app/clock.py` docstring 声称存在的那条扫描用例，现在真的存在了。

    落库一律 naive UTC（`clock.now_naive()`），对外标时区用 `clock.now_aware()`，
    给人看的本地串用 `clock.now_local()`。直接写 `datetime.now()` 会把
    aware/naive 混进同一张表——阶段二 `/api/monitor/overview` 就是这么炸的。
    """
    hits = [
        *_scan_dotted(APP, "datetime.now", skip={CLOCK_SELF}),
        *_scan_dotted(APP, "datetime.utcnow", skip={CLOCK_SELF}),
        *_scan_dotted(APP, "datetime.today", skip={CLOCK_SELF}),
    ]
    assert hits == [], (
        "app/ 下不得裸调 datetime.now()/utcnow()，请走 app/clock.py：\n  "
        + "\n  ".join(hits)
    )


def test_扫描面自证_clock_py_自己确实被数到():
    """防空转：上一条排除了 `app/clock.py`，得证明排除的是**有东西**的那一格。

    没有这条，判据哪天写歪（比如后缀匹配失效）会让扫描恒为空集，
    而恒为空集的闸门永远报绿。
    """
    inside = _scan_dotted(APP, "datetime.now")
    assert len([h for h in inside if h.startswith(CLOCK_SELF)]) == 3, inside


# ---------------------------------------------------------------- §2 来源棘轮

#: `app/` 里直接调 `date.today()`（绕过 `clock.today()`）的处数。**零基线。**
#:
#: 2026-09-11 由 69 收敛到 0（P1-53）。收敛的动机不是"整齐"，是**能冻得住**：
#: 想在测试里把业务日期钉死（把 CI 跨午夜那条窗口真正归零），
#: 得先有一个能冻的入口；只要还有 69 处各自去问系统时钟，冻结就只能冻住其中一部分，
#: 比不冻更难排查。
DATE_TODAY_BASELINE = 0


def test_app_内直接调_date_today_的处数只减不增():
    hits = _scan_dotted(APP, "date.today", skip={CLOCK_SELF})
    assert len(hits) <= DATE_TODAY_BASELINE, (
        f"绕过 clock.today() 的地方变多了（{DATE_TODAY_BASELINE} → {len(hits)}）：\n  "
        + "\n  ".join(hits)
    )
    assert len(hits) == DATE_TODAY_BASELINE, (
        f"欠账减少了（{DATE_TODAY_BASELINE} → {len(hits)}），"
        "请把 DATE_TODAY_BASELINE 一并改小——基线不跟着减，就不再表示还欠多少。"
    )


def test_app_内不得把_today_直接_import_进来():
    """`from ..clock import today` 也要禁——它**逃得过冻结**。

    `clock.today()` 是每次调用时在模块对象上取属性，所以把 `app.clock.today` 换掉
    就能一次覆盖全部调用点；而 `from ..clock import today` 在 import 那一刻就把函数
    **绑成了本模块的名字**，之后再换 `app.clock.today` 对它无效。

    这不是理论：收敛前 `app/routers/surgery.py` 正是这么写的，
    一个模块就足以让"冻住业务日期"变成"冻住了大部分"——
    而**冻住了大部分比没冻更难排查**：你会以为时间是固定的。
    """
    offenders = []
    for path in _py_files(APP):
        rel = _rel(path)
        if rel == CLOCK_SELF:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("clock"):
                for alias in node.names:
                    if alias.name in ("today", "today_str"):
                        offenders.append(f"{rel}:{node.lineno} → from …clock import {alias.name}")
    assert offenders == [], (
        "这些地方把 today/today_str 直接 import 进来了，冻结覆盖不到；"
        "请改成 `from … import clock` + `clock.today()`：\n  " + "\n  ".join(offenders)
    )


def test_冻结了业务日期的用例档不得再裸调_date_today():
    """**零基线。** 冻结与裸 `date.today()` 是一对会互相拆台的组合。

    实测取证（2026-09-11，本轮当场踩到）：给 `test_spd_population_contract.py`
    加上 `freeze_business_date(2026-06-15)` 之后五条用例当场红了——

        {'period_end': '2026-07-15'} != {'period_end': '2026-10-11'}

    服务端从冻住的 `service_start` 推算（06-15 + 30 天），而用例里那行
    `date.today() + timedelta(days=30)` 问的是**真实时钟**。两边不再是同一把尺子。

    所以规则不是"测试里不许用 `date.today()`"（不冻结的档用它没问题，
    全仓还有一批），而是**冻了就不许再绕过入口**——判据必须与被判对象同源，
    走 `conftest.business_today()`（它已接到 `app.clock.today` 上）。
    """
    offenders = []
    for path in _py_files(TESTS):
        src = path.read_text(encoding="utf-8")
        if "freeze_business_date" not in src:
            continue
        tree = ast.parse(src)
        aliases = _alias_map(tree)
        for node in ast.walk(tree):
            if _dotted_clock(node, aliases) == "date.today":
                offenders.append(f"{_rel(path)}:{node.lineno}")
    assert offenders == [], (
        "这些用例档冻住了业务日期，却还在裸调 date.today()——判据与被判对象会取自"
        "两把不同的尺子。请改用 conftest.business_today()：\n  " + "\n  ".join(offenders)
    )


def test_自证_确实有档在用冻结():
    """防空转：上一条的分母是"用了 freeze_business_date 的档"。

    一个档都没有的话，那条规则恒为真——绿得毫无意义。
    """
    users = [
        _rel(p) for p in _py_files(TESTS)
        if "freeze_business_date(" in p.read_text(encoding="utf-8")
        and p.name not in ("conftest.py", "test_clock.py")
    ]
    assert len(users) >= 4, f"在用冻结的档只有 {users}，规则的分母可能空了"


# ---------------------------------------------------------------- §3 取值时刻


def _import_time_nodes(tree: ast.Module):
    """import 那一刻真正会执行到的节点。

    函数体**不算**（它在调用时才执行，那正是我们希望的"现取"）；但函数的
    **默认值**与**装饰器参数**算——`def f(d=date.today())` 与
    `@parametrize("d", [date.today()])` 都在 import 时求值，是同一个缺陷，
    只按"模块顶层赋值"判会整族漏掉。类体算（`class C: TODAY = ...` 同样是快照）。
    """
    out: list[ast.AST] = []

    def take(node):
        out.extend(ast.walk(node))   # walk 已含 node 自身，别再 append 一次（会重复计数）

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for deco in child.decorator_list:
                    take(deco)
                defaults = child.args.defaults + [d for d in child.args.kw_defaults if d]
                for default in defaults:
                    take(default)
                continue
            if isinstance(child, ast.Lambda):
                continue
            out.append(child)
            walk(child)

    walk(tree)
    return out


def _snapshots_in_tree(tree: ast.Module) -> list[tuple[str, str]]:
    """返回 [(承载它的顶层变量名, 取的是什么)]。

    以变量名而非行号作键：行号会因为上面加了几行注释就变，键跟着变的清单
    每次改动都要人工对账，最后没人愿意维护它。
    """
    owner: dict[int, str] = {}
    for stmt in ast.walk(tree):
        target = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
        elif isinstance(stmt, ast.AnnAssign):
            target = stmt.target
        if isinstance(target, ast.Name):
            for sub in ast.walk(stmt):
                owner[id(sub)] = target.id
    aliases = _alias_map(tree)
    out = []
    for node in _import_time_nodes(tree):
        hit = _any_clock(node, aliases)
        if hit:
            out.append((owner.get(id(node), "<module>"), hit))
    return out


def _snapshots(root: pathlib.Path):
    """返回 [(文件, 顶层变量名, 取的是什么)]。"""
    return [
        (_rel(path), name, kind)
        for path in _py_files(root)
        for name, kind in _snapshots_in_tree(ast.parse(path.read_text(encoding="utf-8")))
    ]


#: `app/` 侧唯一豁免，且理由是**永久性**的：进程启动时刻按定义就该在 import
#: 时取一次，它不是"业务今天"，不需要跟着时钟走。其余任何一处都得现取。
#: 只减不增。
APP_IMPORT_TIME_OK = {
    ("app/monitor.py", "STARTED_AT"): "进程启动时刻，运行时长的基准，本就该只取一次",
}


def test_tests_内不得在模块顶层快照时间():
    """CI run 552 的直接成因，零基线。

    七个测试文件写着 `TODAY = date.today().isoformat()`，在 import 那一刻取值；
    服务端在请求发生时才算。那一轮跑了 62 分钟、23:42 开跑，跨过午夜，
    12 条用例整齐地差一天。重跑一次就绿——这正是它被当成 flake 的原因。

    取值改到用例里现取之后，两个时刻之间只剩毫秒级的窗口。
    模块级夹具里取也算现取（窗口是该模块的时长，不是整轮的时长）。
    """
    hits = _snapshots(TESTS)
    assert hits == [], (
        "测试里的时间基准请现取（见 conftest.business_today / utc_today_str）：\n  "
        + "\n  ".join(f"{f}:{name} → {kind}" for f, name, kind in hits)
    )


def test_app_内的顶层时间快照只有写明理由的那一处():
    hits = {(f, name) for f, name, _ in _snapshots(APP)}
    unexpected = sorted(hits - set(APP_IMPORT_TIME_OK))
    assert unexpected == [], (
        "app/ 模块顶层新增了时间快照。长驻进程里这种值第二天就是错的，"
        "除非它本来就该只取一次（那请写进 APP_IMPORT_TIME_OK 并说明理由）：\n  "
        + "\n  ".join(f"{f}:{name}" for f, name in unexpected)
    )


def test_豁免清单不许腐烂():
    """修好了却留在清单里的条目要立刻清掉——否则清单会慢慢变成一张摆设。"""
    hits = {(f, name) for f, name, _ in _snapshots(APP)}
    stale = sorted(set(APP_IMPORT_TIME_OK) - hits)
    assert stale == [], f"这些豁免已经不对应任何实际命中，请删掉：{stale}"


def test_扫描面自证_确实读到了测试文件():
    assert len(_py_files(TESTS)) > 100, "tests/ 扫描面异常，闸门可能在空转"


@pytest.mark.parametrize("src, expected", [
    # —— 该抓的四种形状 ——
    ("TODAY = date.today()", [("TODAY", "date.today")]),
    ("class C:\n    TODAY = date.today()", [("TODAY", "date.today")]),
    ("def f(d=date.today()):\n    pass", [("<module>", "date.today")]),
    ("@mark.parametrize('d', [date.today()])\ndef f():\n    pass",
     [("<module>", "date.today")]),
    # —— 不该误伤的三种 ——
    ("from datetime import date as d\nTODAY = d.today()", [("TODAY", "date.today")]),
    ("import datetime as dt\nNOW = dt.datetime.now()", [("NOW", "datetime.now")]),
    ("def f():\n    return date.today()", []),
    ("NOW = date.today          # 存的是函数对象，不是取值", []),
    ("import datetime\nX = datetime.min.time()  # 常量，不是读时钟", []),
])
def test_取值时刻判据的边界(src, expected):
    """放宽判据必须同时钉住不误伤，否则它会先被加豁免、再被加得没人看。

    走的是 `_snapshots_in_tree` 本身，不另写一份等价实现——两份实现迟早会飘，
    那时这条边界用例会在真判据已经坏掉的情况下继续报绿。
    """
    assert _snapshots_in_tree(ast.parse(src)) == expected


# ---------------------------------------------------------------- §4 helper 自身


def test_conftest_的三个取时间_helper_都是现取():
    """它们必须是**函数**，每次调用都重问时钟——否则只是换了个名字的快照。

    `business_today()` 现在走 `app.clock.today`（P1-53 之后的唯一入口），
    所以这里用 `freeze_business_date()` 来证明"现取"：冻住之后取到的是冻住的值，
    离开之后又回到真实时钟。**这同时也证明了判据与被判对象走的是同一个入口**——
    如果 conftest 还自己去问 `date.today()`，冻住服务端会让两边对不上，比不冻更糟。
    """
    import datetime as _dt

    before = business_today()
    frozen = before + _dt.timedelta(days=1)
    with freeze_business_date(frozen):
        assert business_today() == frozen
        assert business_today_str() == frozen.isoformat()
    assert business_today() == before


def test_utc_那个_helper_也是现取():
    """UTC 那一支不在冻结范围内（只冻日期不冻时间戳），所以单独用打桩证明现取。"""
    import datetime as _dt

    import conftest

    class _Tomorrow(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime.now(tz) + _dt.timedelta(days=1)

    expected = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    original = conftest.datetime
    try:
        conftest.datetime = _Tomorrow
        assert utc_today_str() == expected
    finally:
        conftest.datetime = original


def test_两个今天各自对应服务端的哪一把尺子():
    """`business_today()` 对应 `clock.today()`（本地业务日期），
    `utc_today_str()` 对应 `models.utcnow()` 落库时间戳切出的 UTC 日。

    runner 时区是 UTC，两者在 CI 上恒等——所以挑错了 CI 也照样绿，
    只有东八区的开发机会在早八点前红。这条把两者的**定义**钉住，
    至于哪个用例该用哪个，只能靠 conftest 里那段 docstring 说清楚。
    """
    import datetime as _dt

    from app import clock

    assert business_today() == clock.today()
    assert business_today_str() == clock.today_str()
    assert utc_today_str() == _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
