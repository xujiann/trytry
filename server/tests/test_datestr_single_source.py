"""日期与月份入参的口径只有一个真源：`app/datetypes.py`。别处再写一遍就变红。

`datetypes.py` 建立的时候，全平台有 22 处日期字段各自写着
`Field(pattern=r"^\\d{4}-\\d{2}-\\d{2}$")`。那个正则**只管形状不管日历**：
`2026-02-31` 照过，入库之后各处统计 `strptime` 解析失败就 `continue`，
于是一条用假日期建的派驻**无声无息地从下沉指标里消失**（见 datetypes.py 开头）。

22 处已经全部收敛到 `DateStr` / `OptionalDateStr`，现在欠账是 0。但**没有任何
东西拦着第 23 处**——下一个人照抄邻居的写法，又写一遍那个正则，行为回到
"形状对就放行"，而所有测试照绿。这就是本轮要找的坏清单/坏纪律的形状：
正确性依赖每个人记得用哪个类型，忘记的后果是静默的。

**月份口径（P1-34）走的是同一条判定，所以扩在同一份守卫里、不另起一个文件。**
`YYYY-MM` 曾有 5 处各写一遍 `pattern=r"^\\d{4}-\\d{2}$"`，`2026-13` / `2026-00`
一路放行：薪酬/财务凭空多出一期对不回去的数，按 `strftime("%Y-%m")` 归属的
统计则永远匹配不到它，报表安静地少一段。判定是同一条——"入参的日期/周期形状
只许在真源里拼写一次"——若拆成两个文件，AST 扫描、覆盖面自证、违规报错
就要各维护一份，那正是本仓库上一轮花整轮消灭的形状。

推导而非枚举：真源就是 `datetypes` 里的那两条正则本身，本文件从模块里读出来，
再去 `app/` 里找"别处又写了一遍同样形状"的字面量。规则跟着代码走，
不需要维护任何"哪些字段是日期/周期"的清单。

**这道闸门认不出什么**（写在明处，别让人误以为它守住了全部）：不含形状字面量的
等价写法——手工 `period.split("-")` + `int()`、`len(period) == 7`、直接交给
`strptime` 的宽松解析。它们同样会放行 `2026-13`，但没有可扫描的字面量特征，
只能靠 review 与类型收敛来防。
"""
from __future__ import annotations

import ast
import pathlib
import re
import warnings

from app import datetypes

SERVER_DIR = pathlib.Path(__file__).resolve().parents[1]
APP_DIR = SERVER_DIR / "app"
#: 真源文件：这两条正则在这里定义，只有这里可以出现。
SOURCE_OF_TRUTH = APP_DIR / "datetypes.py"

#: "又写了一遍完整日期形状"的各种等价写法（转义差异、字符组写法、有无锚点）。
FULL_DATE_SHAPES = (
    re.compile(r"\\d\{4\}-\\d\{2\}-\\d\{2\}"),
    re.compile(r"\[0-9\]\{4\}-\[0-9\]\{2\}-\[0-9\]\{2\}"),
    re.compile(r"\\d\{4\}-\\d\{1,2\}-\\d\{1,2\}"),
)

#: "又写了一遍月份形状"的等价写法。尾部的 `(?!-)` 是为了不把上面那条完整日期
#: 形状重复算成一次月份违规——`\d{4}-\d{2}-\d{2}` 的前半段长得就是月份形状，
#: 两条规则各报各的才说得清是哪一类欠账。
MONTH_SHAPES = (
    re.compile(r"\\d\{4\}-\\d\{2\}(?!-)"),
    re.compile(r"\[0-9\]\{4\}-\[0-9\]\{2\}(?!-)"),
    re.compile(r"\\d\{4\}-\\d\{1,2\}(?!-)"),
)


def _source_files() -> list[pathlib.Path]:
    return [
        p for p in sorted(APP_DIR.rglob("*.py"))
        if "__pycache__" not in p.parts and p != SOURCE_OF_TRUTH
    ]


def _string_literals(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def _offenders(shapes: tuple[re.Pattern[str], ...]) -> list[str]:
    out = []
    for path in _source_files():
        for lineno, value in _string_literals(path):
            if any(shape.search(value) for shape in shapes):
                out.append(f"{path.relative_to(SERVER_DIR)}:{lineno}: {value[:60]!r}")
    return out


def _type_usage(pattern: str) -> int:
    """某一族入参类型的引用处数——已经收敛的那一面有多大。"""
    count = 0
    for path in _source_files():
        count += len(re.findall(pattern, path.read_text(encoding="utf-8")))
    return count


#: 两族入参类型的引用计数用的匹配式（放在 f-string 外：反斜杠不能进 f-string）。
_DATE_TYPE_REFS = r"\b(?:Optional)?DateStr\b"
_PERIOD_TYPE_REFS = r"\b(?:(?:Optional)?PeriodStr|is_period)\b"


def test_覆盖面自证():
    files = _source_files()
    literals = sum(1 for p in files for _ in _string_literals(p))
    date_refs = _type_usage(_DATE_TYPE_REFS)
    period_refs = _type_usage(_PERIOD_TYPE_REFS)
    summary = "\n".join([
        "",
        "[日期/月份口径单一真源守卫] 覆盖面自证",
        f"  真源：app/datetypes.py 的 _SHAPE = {datetypes._SHAPE.pattern!r}",
        f"        app/datetypes.py 的 PERIOD_SHAPE = {datetypes.PERIOD_SHAPE.pattern!r}",
        "        （本文件不另抄一份，从模块读出来对照）",
        f"  扫描：{len(files)} 个 .py / {literals} 条字符串字面量"
        "（app/ 全量，除真源文件本身，无抽样、无跳过）",
        f"  违规·日期（别处又写了一遍完整日期正则）：{len(_offenders(FULL_DATE_SHAPES))} 处",
        f"  违规·月份（别处又写了一遍月份正则）：{len(_offenders(MONTH_SHAPES))} 处",
        f"  已收敛：DateStr / OptionalDateStr 共 {date_refs} 处引用",
        f"          PeriodStr / OptionalPeriodStr / is_period 共 {period_refs} 处引用",
        "  认不出的形态：手工 split(\"-\")+int()、len(period)==7、strptime 宽松解析"
        "（无字面量特征，见模块 docstring）",
    ])
    print(summary)
    warnings.warn(summary, UserWarning, stacklevel=2)
    assert files and literals, "扫描范围为空 = 这道闸门什么也没守"


def test_日期正则不得在别处重写():
    offenders = _offenders(FULL_DATE_SHAPES)
    assert offenders == [], (
        "以下位置又写了一遍完整日期正则——正则只管形状不管日历，"
        "`2026-02-31` 会照样入库，然后在统计里被 strptime 静默丢掉"
        "（见 app/datetypes.py 开头记的那条派驻）：\n  " + "\n  ".join(offenders)
        + "\n\n请改用 datetypes.DateStr / OptionalDateStr（做真实日历校验）。"
    )


def test_月份正则不得在别处重写():
    offenders = _offenders(MONTH_SHAPES)
    assert offenders == [], (
        "以下位置又写了一遍月份正则——`\\d{4}-\\d{2}` 放行 `2026-13` / `2026-00`，"
        "脏周期入库后按 %Y-%m 归属的统计永远匹配不到它，报表安静地少一段"
        "（P1-34，与上面那条日期缺陷同族）：\n  " + "\n  ".join(offenders)
        + "\n\n请改用 datetypes.PeriodStr / OptionalPeriodStr（模型字段），"
        "或 datetypes.is_period（查询参数自己抛 HTTPException 时）。"
    )


def test_月份形状在_deps_里指向真源():
    """`deps.period_bounds` 也要判月份形状，它必须指向真源，不能自带一条。

    这条与上面的字面量扫描分工：扫描抓"deps 里又出现了一条月份正则字面量"
    （实测能抓到），这条抓"deps 的月份形状悄悄变成了别的东西"——改真源时
    它必须跟着变，而不是各走各的。

    **老实说清它抓不到什么**：`re` 模块对相同 pattern 有编译缓存，
    `re.compile(PERIOD_SHAPE.pattern)` 返回的就是同一个对象，所以 `is` 也分辨不出
    "复用"与"又 compile 了一遍同一串"。那种写法与复用完全等价（同一条规则、
    改真源照样跟着变），不是这道闸门要拦的东西。
    """
    from app import deps

    assert deps._ASCII_MONTH is datetypes.PERIOD_SHAPE


def test_真源本身仍在做日历校验():
    """守卫盯着"别处别写"，这条盯着"真源别退化"——两条都在，规则才闭合。

    真源如果被人改回纯正则（去掉 `date.fromisoformat`），上面那条照样绿，
    而 2 月 31 日又回来了。所以直接对真源做一次行为断言。
    """
    from pydantic import BaseModel, ValidationError

    class _Probe(BaseModel):
        day: datetypes.DateStr
        maybe: datetypes.OptionalDateStr = ""

    assert _Probe(day="2026-02-28").day == "2026-02-28"
    assert _Probe(day="2026-02-28", maybe="").maybe == ""
    for bad in ("2026-02-31", "20260228", "2026-W07-1", "2026-2-8", ""):
        try:
            _Probe(day=bad)
        except ValidationError:
            continue
        raise AssertionError(f"DateStr 放行了非法日期 {bad!r}——日历校验已退化")


def test_月份真源本身仍在做日历校验():
    """同上，盯的是 `PeriodStr` 不被改回"形状对就放行"。"""
    from pydantic import BaseModel, ValidationError

    class _Probe(BaseModel):
        period: datetypes.PeriodStr
        maybe: datetypes.OptionalPeriodStr = ""

    assert _Probe(period="2026-02").period == "2026-02"
    assert _Probe(period="2026-12", maybe="").maybe == ""
    assert _Probe(period="2026-12", maybe="1999-01").maybe == "1999-01"
    # 日历：13 月与 0 月不存在；年份：0000 与 9999 在受理范围外
    # （9999-12 的次月首日会溢出 datetime.max，下游算"次月"的地方会 500）
    for bad in ("2026-13", "2026-00", "0000-01", "9999-12", "202612",
                "2026-1", "2026-01-01", "2026", "２０２６-０１", " 2026-01", ""):
        try:
            _Probe(period=bad)
        except ValidationError:
            continue
        raise AssertionError(f"PeriodStr 放行了非法周期 {bad!r}——日历校验已退化")


def test_is_period_与_PeriodStr_是同一条判定():
    """查询参数走谓词、模型字段走类型，但**判定必须是同一条**。

    两个出口各判一次的话，`?period=2026-13` 与 body 里的 `2026-13` 迟早会漂移成
    一个 422 一个 200——那正是 P1-34 之前的现场（body 放行、query 也放行）。
    """
    from pydantic import BaseModel, ValidationError

    class _Probe(BaseModel):
        period: datetypes.PeriodStr

    for value in ("2026-01", "1999-01", "2100-12", "2026-13", "2026-00",
                  "9999-12", "0000-01", "2026-1", "bad", "", "2026-01-01"):
        try:
            _Probe(period=value)
            accepted_by_type = True
        except ValidationError:
            accepted_by_type = False
        assert accepted_by_type == datetypes.is_period(value), (
            f"{value!r}：PeriodStr 与 is_period 判定不一致——两个出口已经漂移"
        )
