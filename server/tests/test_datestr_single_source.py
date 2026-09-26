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

import pytest

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
    for bad in ("2026-02-31", "20260228", "2026-W07-1", "2026-2-8", "", "2026-02-28\n"):
        try:
            _Probe(day=bad)
        except ValidationError:
            continue
        raise AssertionError(f"DateStr 放行了非法日期 {bad!r}——日历校验已退化")
    # 末尾换行是形状问题不是日历问题：`$` 放过换行、`fullmatch` 不放（/review 指出）
    with pytest.raises(ValueError, match="格式"):
        datetypes._check("2026-02-28\n", allow_blank=False)


# ---------------------------------------------------------------- 请求体里的裸 str 日期字段（P1-61）
#
# 上面那条守卫盯的是"别处又写了一遍日期正则"。可 D-3 当年收敛的是**写了日期正则的
# 22 处**——从来没写过正则、只有 `str`（或 `min_length=10, max_length=10`）的请求体
# 日期字段，不在它的分母里，**也不在这条守卫的视野里**。2026-09-24 实测：
#
#   凭证 voucher_date="2026/09/24"、对接方不带 period → 201，period 由 voucher_date[:7] 推出成
#       "2026/09"，过账后**不在 2026-09 的试算平衡里**（独占一个不存在的会计期间；凭证页会带上
#       当前期间，走页面不落假期间，但日期串照样原样入库）；
#   凭证 voucher_date="2026-02-31" → 201；
#   患者 birth_date="1987/01/01" → 201 原样入库；
#   县外就诊 visit_date="abcdefghij" → 201（长度恰好 10）；
#   随访 due_date="2026-02-31" → 201。
#
# 判据是推导的：先找出所有 pydantic 模型（BaseModel 的子孙），再找**路由函数参数里出现的**
# 模型（请求体）及其嵌套/继承的模型——响应模型只出现在 `response_model=` 里，不会被数进来。
# 字段名按词元匹配 `date`（`date` / `xxx_date` / `date_xxx`），不误伤 `update`、`candidate`。


#: 请求体里注解为裸 `str` 的日期字段（P1-61）。**只许变少**：改成 `DateStr` /
#: `OptionalDateStr` 一个划掉一个。改之前逐个查写入方（三端前端、HL7/FHIR 适配器、
#: 导入脚本、种子）实际发什么。2026-09-24 清零（22 → 0）；空集合照样是棘轮，
#: 新增一个裸 `str` 的请求体日期字段即红。
KNOWN_BARE_BODY_DATE_FIELDS: set[str] = set()

#: 词元 `date` 之外还认 `due`（P2-55）：「下次随访日」叫 next_due，原先整个不在视野里，裸 str 照收「2026/10/1」。
#: 还认 `lmp` / `edc`（P2-231）：末次月经、预产期按产科惯例用缩写命名，孕产妇建册的这两个日期原先同样不在视野里
_DATE_TOKEN = re.compile(r"(^|_)(date|due|lmp|edc)($|_)")
_ROUTE_DIRS = (APP_DIR / "routers", APP_DIR / "spd" / "routers")
_HTTP_VERBS = ("get", "post", "put", "patch", "delete")


def _names_in(node) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _model_classes() -> dict[str, list[tuple[pathlib.Path, ast.ClassDef]]]:
    """app/ 里 BaseModel 的全部子孙，按类名归组（同名类在不同模块各算一个）。"""
    classes: dict[str, list] = {}
    bases: dict[str, set[str]] = {}
    for path in sorted(APP_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                classes.setdefault(node.name, []).append((path, node))
                bases.setdefault(node.name, set()).update(
                    ast.unparse(b).split(".")[-1] for b in node.bases
                )
    models = {"BaseModel"}
    grew = True
    while grew:
        grew = False
        for name, parents in bases.items():
            if name not in models and parents & models:
                models.add(name)
                grew = True
    models.discard("BaseModel")
    return {name: classes[name] for name in models}


def _request_models(models) -> set[str]:
    """路由函数参数里出现的模型，加上它们字段里嵌套的、以及它们继承的模型（传递闭包）。"""
    roots: set[str] = set()
    for base in _ROUTE_DIRS:
        for path in sorted(base.rglob("*.py")):
            for func in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not any(
                    isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and d.func.attr in _HTTP_VERBS
                    for d in func.decorator_list
                ):
                    continue
                for arg in list(func.args.args) + list(func.args.kwonlyargs):
                    if arg.annotation is not None:
                        roots |= _names_in(arg.annotation) & models.keys()
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        for _path, cls in models[name]:
            stack.extend({ast.unparse(b).split(".")[-1] for b in cls.bases} & models.keys())
            for stmt in cls.body:
                if isinstance(stmt, ast.AnnAssign):
                    stack.extend(_names_in(stmt.annotation) & models.keys())
    return seen


def _bare_body_date_fields() -> set[str]:
    models = _model_classes()
    out = set()
    for name in _request_models(models):
        for path, cls in models[name]:
            for stmt in cls.body:
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and _DATE_TOKEN.search(stmt.target.id)
                    and ast.unparse(stmt.annotation) in ("str", "str | None", "Optional[str]")
                ):
                    out.add(f"{path.relative_to(APP_DIR).as_posix()}::{name}.{stmt.target.id}")
    return out


def test_请求体日期字段判据自证():
    models = _model_classes()
    requests = _request_models(models)
    print(f"\n[请求体日期字段] pydantic 模型 {len(models)} 个，其中作请求体（含嵌套/继承）"
          f" {len(requests)} 个；裸 str 日期字段 {len(_bare_body_date_fields())} 处")
    assert len(requests) >= 200, "请求模型数得太少，扫描面可能不对"
    # 请求体：嵌套进来的也要数到（凭证分录是凭证请求体里的 list[EntryIn]）
    assert {"VoucherIn", "EntryIn", "PatientCreate"} <= requests
    # 响应模型不算：它们只出现在 response_model= 里（这几个名字里带 date 字段、又是裸 str）
    assert not {"ArchivePatient", "ArchivePhysicalExam", "ReferralFeedItem"} & requests
    # 词元匹配：不误伤 update / candidate 这类恰好含 date 字母的名字
    assert not _DATE_TOKEN.search("last_update") and not _DATE_TOKEN.search("candidate_ids")
    assert _DATE_TOKEN.search("date") and _DATE_TOKEN.search("visit_date")
    assert _DATE_TOKEN.search("next_due") and not _DATE_TOKEN.search("overdue_count")


def test_不得新增裸str的请求体日期字段():
    new = sorted(_bare_body_date_fields() - KNOWN_BARE_BODY_DATE_FIELDS)
    assert new == [], (
        "以下请求体日期字段是裸 `str`——`2026/09/24`、`2026-02-31`、`abcdefghij`（恰好 10 个字符）"
        "都会原样入库：\n  " + "\n  ".join(new)
        + "\n\n请改用 datetypes.DateStr（必填）/ OptionalDateStr（可空）。"
    )


def test_请求体日期字段名单只许变少():
    stale = sorted(KNOWN_BARE_BODY_DATE_FIELDS - _bare_body_date_fields())
    assert stale == [], (
        "这些已经改成 DateStr（或已不存在）了，请从 KNOWN_BARE_BODY_DATE_FIELDS 划掉：\n  "
        + "\n  ".join(stale)
    )


# ---------------------------------------------------------------- 请求体时间戳字段（P1-100）
#
# 同一个坑的时间戳版：17 个「某时某分」的请求体字段原先是 `max_length=16/19` 的裸 `str`，界面上是自由文本框。
# 形状不一的值照存之后，按字符串比较的定时宣教派发晚 9 天 / 当场就发 / 当年不发，按字符串排序的体温单把
# 「8:00」排到「14:00」之后，解析不了的冷缺血时间从质控指标里消失（修前实测，见 TECH_DEBT P1-100）。
# 判据按名字认（词元 at / time / datetime / ts / timestamp），裸 `str` 且既没有 `pattern` 也没有
# `field_validator` 即算；零基线。

_STAMP_TOKEN = re.compile(r"(^|_)(at|time|datetime|ts|timestamp)($|_)")

#: 欠账名单（只减不增）：2026-09-24 实测 13 处 → 0（同批换 `DateTimeStr` / `DateTimeSecStr` 及可空版）
KNOWN_BARE_BODY_STAMP_FIELDS: set[str] = set()

#: 名字像时间戳、按设计不走 `DateTimeStr` 的字段 → 理由（只减不增，过期即红）
STAMP_BY_DESIGN: dict[str, str] = {
    "spd/routers/care.py::MeasurementIn.measured_at":
        "处理函数里 `datetime.fromisoformat` 解析、失败 422，落 `DateTime` 列——不是按字符串比较的字符串列",
    "schemas.py::SlotCreate.slot_time": "号源时段标签（「09:00-10:00」），不是时间戳",
    "appointments.py::SlotTemplate.slot_time": "号源时段标签（「09:00-10:00」），不是时间戳",
}


def _field_validated(cls: ast.ClassDef, field: str) -> bool:
    """类里有 `@field_validator("field", …)` 管着这个字段。"""
    for stmt in cls.body:
        if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in stmt.decorator_list:
            if (isinstance(deco, ast.Call) and ast.unparse(deco.func).split(".")[-1] == "field_validator"
                    and any(isinstance(a, ast.Constant) and a.value == field for a in deco.args)):
                return True
    return False


def _bare_body_stamp_fields() -> set[str]:
    models = _model_classes()
    out = set()
    for name in _request_models(models):
        for path, cls in models[name]:
            for stmt in cls.body:
                if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                        and _STAMP_TOKEN.search(stmt.target.id)
                        and ast.unparse(stmt.annotation) in ("str", "str | None", "Optional[str]")):
                    continue
                call = stmt.value if isinstance(stmt.value, ast.Call) else None
                if call is not None and any(k.arg == "pattern" for k in call.keywords):
                    continue
                if _field_validated(cls, stmt.target.id):
                    continue
                rel = path.relative_to(APP_DIR).as_posix().removeprefix("routers/")
                out.add(f"{rel}::{name}.{stmt.target.id}")
    return out


def test_请求体时间戳字段不得是裸str():
    bad = sorted(_bare_body_stamp_fields() - STAMP_BY_DESIGN.keys() - KNOWN_BARE_BODY_STAMP_FIELDS)
    assert bad == [], (
        "以下请求体时间戳字段是裸 `str`——「2026-10-1 8:00」「10月1日」「2026/10/01 08:00」都会原样入库，"
        "按字符串比较 / 排序的消费方对它失效：\n  " + "\n  ".join(bad)
        + "\n\n请改用 datetypes.DateTimeStr / OptionalDateTimeStr（`String(16)` 列）或 "
        "DateTimeSecStr / OptionalDateTimeSecStr（`String(19)` 列）；按设计不走的写进 STAMP_BY_DESIGN 并写明理由。"
    )


def test_时间戳名单只许变少():
    stale = sorted((STAMP_BY_DESIGN.keys() | KNOWN_BARE_BODY_STAMP_FIELDS) - _bare_body_stamp_fields())
    assert stale == [], (
        "这些已经不是裸 str（或已不存在）了，请从 STAMP_BY_DESIGN / KNOWN_BARE_BODY_STAMP_FIELDS 划掉：\n  "
        + "\n  ".join(stale)
    )


def test_时间戳判据自证():
    # 词元匹配：认得出 recorded_at / slot_time / send_at，不误伤 category / status / latest
    assert all(_STAMP_TOKEN.search(n) for n in ("recorded_at", "slot_time", "send_at", "push_time", "at"))
    assert not any(_STAMP_TOKEN.search(n) for n in ("category", "status", "latest", "format", "stats"))
    # pattern / field_validator 管着的不报：推送时点（P2-53 的 pattern）、急诊绿道节点（L-12 的校验器）
    found = _bare_body_stamp_fields() | STAMP_BY_DESIGN.keys()
    assert "spd/routers/followup.py::ReportTaskIn.push_time" not in found
    assert "emergency.py::MilestoneCreate.occurred_at" not in found
    # 真源本身：合法值原样返回（不改写分隔符）、只到日期也收、秒按精度收、非法值带人话
    assert datetypes.check_datetime("2026-10-01T08:00", seconds=False) == "2026-10-01T08:00"
    assert datetypes.check_datetime("2026-10-01", seconds=False) == "2026-10-01"
    assert datetypes.check_datetime("2026-10-01 08:00:59") == "2026-10-01 08:00:59"
    for bad, seconds in (("2026-10-01 08:00:00", False), ("2026-10-1 8:00", True), ("２０２６-10-01 08:00", True),
                         ("2026-02-30 08:00", True), ("2026-10-01 24:00", True), ("2026-10-01 08:00:60", True),
                         ("2026-10-01T", True), ("2026-10-01 08", True)):
        with pytest.raises(ValueError):
            datetypes.check_datetime(bad, seconds=seconds)
