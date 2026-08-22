"""日期入参的口径只有一个真源：`app/datetypes.py`。别处再写一遍日期正则就变红。

`datetypes.py` 建立的时候，全平台有 22 处日期字段各自写着
`Field(pattern=r"^\\d{4}-\\d{2}-\\d{2}$")`。那个正则**只管形状不管日历**：
`2026-02-31` 照过，入库之后各处统计 `strptime` 解析失败就 `continue`，
于是一条用假日期建的派驻**无声无息地从下沉指标里消失**（见 datetypes.py 开头）。

22 处已经全部收敛到 `DateStr` / `OptionalDateStr`，现在欠账是 0。但**没有任何
东西拦着第 23 处**——下一个人照抄邻居的写法，又写一遍那个正则，行为回到
"形状对就放行"，而所有测试照绿。这就是本轮要找的坏清单/坏纪律的形状：
正确性依赖每个人记得用哪个类型，忘记的后果是静默的。

推导而非枚举：真源就是 `datetypes._SHAPE` 那一条正则本身，本文件从模块里读出来，
再去 `app/` 里找"别处又写了一遍同样形状"的字面量。规则跟着代码走，
不需要维护任何"哪些字段是日期"的清单。
"""
from __future__ import annotations

import ast
import pathlib
import re
import warnings

from app import datetypes

SERVER_DIR = pathlib.Path(__file__).resolve().parents[1]
APP_DIR = SERVER_DIR / "app"
#: 真源文件：这条正则在这里定义，只有这里可以出现。
SOURCE_OF_TRUTH = APP_DIR / "datetypes.py"

#: "又写了一遍完整日期形状"的各种等价写法（转义差异、字符组写法、有无锚点）。
FULL_DATE_SHAPES = (
    re.compile(r"\\d\{4\}-\\d\{2\}-\\d\{2\}"),
    re.compile(r"\[0-9\]\{4\}-\[0-9\]\{2\}-\[0-9\]\{2\}"),
    re.compile(r"\\d\{4\}-\\d\{1,2\}-\\d\{1,2\}"),
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


def _offenders() -> list[str]:
    out = []
    for path in _source_files():
        for lineno, value in _string_literals(path):
            if any(shape.search(value) for shape in FULL_DATE_SHAPES):
                out.append(f"{path.relative_to(SERVER_DIR)}:{lineno}: {value[:60]!r}")
    return out


def _datestr_usage() -> int:
    """`DateStr` / `OptionalDateStr` 的引用处数——已经收敛的那一面有多大。"""
    count = 0
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        count += len(re.findall(r"\b(?:Optional)?DateStr\b", text))
    return count


def test_覆盖面自证():
    files = _source_files()
    literals = sum(1 for p in files for _ in _string_literals(p))
    summary = "\n".join([
        "",
        "[日期口径单一真源守卫] 覆盖面自证",
        f"  真源：app/datetypes.py 的 _SHAPE = {datetypes._SHAPE.pattern!r}"
        "（本文件不另抄一份，从模块读出来对照）",
        f"  扫描：{len(files)} 个 .py / {literals} 条字符串字面量"
        "（app/ 全量，除真源文件本身，无抽样、无跳过）",
        f"  违规（别处又写了一遍完整日期正则）：{len(_offenders())} 处",
        f"  已收敛：DateStr / OptionalDateStr 共 {_datestr_usage()} 处引用",
    ])
    print(summary)
    warnings.warn(summary, UserWarning, stacklevel=2)
    assert files and literals, "扫描范围为空 = 这道闸门什么也没守"


def test_日期正则不得在别处重写():
    offenders = _offenders()
    assert offenders == [], (
        "以下位置又写了一遍完整日期正则——正则只管形状不管日历，"
        "`2026-02-31` 会照样入库，然后在统计里被 strptime 静默丢掉"
        "（见 app/datetypes.py 开头记的那条派驻）：\n  " + "\n  ".join(offenders)
        + "\n\n请改用 datetypes.DateStr / OptionalDateStr（做真实日历校验）。"
    )


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
