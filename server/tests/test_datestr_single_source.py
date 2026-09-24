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
#: 导入脚本、种子）实际发什么——`PatientCreate.birth_date` 这类字段可能有外部系统在写。
KNOWN_BARE_BODY_DATE_FIELDS: set[str] = {
    "routers/eldercare.py::AssessmentCreate.assessed_date",
    "routers/homevisits.py::VisitCreate.expect_date",
    "routers/knowledge.py::EntryCreate.expire_date",
    "routers/knowledge.py::EntryUpdate.expire_date",
    "routers/materials.py::ConsumableIn.expire_date",
    "routers/maternal.py::ChildVisitCreate.visit_date",
    "routers/maternal.py::ScreeningCreate.screen_date",
    "routers/maternal.py::VisitCreate.visit_date",
    "routers/maternal.py::WomenHealthCreate.exam_date",
    "routers/publichealth.py::MonitorCreate.record_date",
    "routers/quality.py::InfectionReportCreate.report_date",
    "routers/tcm.py::BatchCreate.expire_date",
    "schemas.py::ContractCreate.signed_date",
    "schemas.py::PatientCreate.birth_date",
}

_DATE_TOKEN = re.compile(r"(^|_)date($|_)")
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
