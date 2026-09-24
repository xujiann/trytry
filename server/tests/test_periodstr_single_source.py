"""月度期间（`YYYY-MM`）入参的口径只有一个真源：`datetypes.PeriodStr` / `deps.require_month`（P1-34）。

`test_datestr_single_source.py` 守的是完整日期；这里是同一个坑的月度版。财务记账、
薪酬发放、基金周期预结的 `period` 与运营月报导出、病历质控统计的查询参数，原先各自写着
`^\\d{4}-\\d{2}$`——正则只管形状不管日历，`2026-13` 照过：入了库就是一条永远对不上任何
月份的记账（月报按 `YYYY-MM` 归集时它不在任何一个月里），进了
`strftime("%Y-%m") == period` 的过滤就是一份**全空却不报错**的报表。

三件事：① 真源本身在做日历校验（别退化回纯正则）；② 五个端点确实换上了真源
（行为回归：`2026-13` 一律 422，合法期间照常进入业务逻辑）；③ 别处不许再写一遍月度
形状的正则（推导：扫 `app/` 全部字符串字面量，真源文件除外）。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest
from pydantic import BaseModel, ValidationError

from app import datetypes
from app.deps import require_month

SERVER_DIR = pathlib.Path(__file__).resolve().parents[1]
APP_DIR = SERVER_DIR / "app"
SOURCE_OF_TRUTH = APP_DIR / "datetypes.py"

#: "又写了一遍月度形状"的等价写法：后面不能紧跟 `-`（那是完整日期，归 DateStr 的守卫管）。
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


def _offenders() -> list[str]:
    out = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if any(shape.search(node.value) for shape in MONTH_SHAPES):
                    out.append(f"{path.relative_to(SERVER_DIR)}:{node.lineno}: {node.value[:60]!r}")
    return out


# ------------------------------------------------------------ ① 真源


class _Probe(BaseModel):
    period: datetypes.PeriodStr


@pytest.mark.parametrize("period", ["2026-01", "2026-12", "2024-02", "0001-01", "9999-12"])
def test_真源放行合法月份(period):
    assert _Probe(period=period).period == period


@pytest.mark.parametrize(
    "period",
    ["2026-13", "2026-00", "2026-1", "202601", "2026-01-01", "2026", "", "0000-01",
     "２０２６-０１", "2026-1a", " 2026-01", "2026-01 ", "2026-01\n"],
)
def test_真源拒绝非法或不存在的月份(period):
    with pytest.raises(ValidationError):
        _Probe(period=period)


class _OptionalProbe(BaseModel):
    period: datetypes.OptionalPeriodStr = ""


def test_可空月度期间_空串放行_非空同样过真源():
    """`OptionalPeriodStr`（P1-61 为凭证的 `period` 加的）：空串表示"没给"、原样放行；
    非空走的是同一个 `check_month`——与 `OptionalDateStr` 对称，别让"可空"变成"不校验"。"""
    assert _OptionalProbe().period == ""
    assert _OptionalProbe(period="").period == ""
    assert _OptionalProbe(period="2026-09").period == "2026-09"
    for bad in ("2026-13", "2026/09", "2026-9", "abc", " 2026-09"):
        with pytest.raises(ValidationError):
            _OptionalProbe(period=bad)


def test_末尾换行按形状错报_不是按月份不存在报():
    """`$` 会放过末尾一个换行，`match` 就先过了形状、再被日历以"月份不存在"拒掉——
    文案对不上真正的毛病（/review 指出）。真源与查询参数形态都要 `fullmatch`。"""
    from fastapi import HTTPException

    with pytest.raises(ValueError, match="格式"):
        datetypes.check_month("2026-12\n")
    with pytest.raises(HTTPException) as exc:
        require_month("2026-12\n")
    assert exc.value.detail == "period 格式须为 YYYY-MM"


def test_真源不是纯正则_日历校验在(monkeypatch):
    """把日历校验拿掉、只留形状，`2026-13` 就又回来了——直接对真源做行为断言。"""
    with pytest.raises(ValueError, match="不存在"):
        datetypes.check_month("2026-13")
    with pytest.raises(ValueError, match="格式"):
        datetypes.check_month("2026-1")
    assert datetypes.check_month("2026-02") == "2026-02"


def test_查询参数形态_文案与状态码():
    from fastapi import HTTPException

    assert require_month("2026-02") == "2026-02"
    with pytest.raises(HTTPException) as shape:
        require_month("2026-1")
    assert (shape.value.status_code, shape.value.detail) == (422, "period 格式须为 YYYY-MM")  # 原文案
    with pytest.raises(HTTPException) as calendar:
        require_month("2026-13")
    assert calendar.value.status_code == 422
    assert calendar.value.detail == "period 2026-13 不存在（月份须为 01~12）"


def test_deps的月度形状不另抄一份():
    """`period_bounds` 用的形状必须就是真源那一个对象（不是长得一样的另一份）。"""
    from app import deps

    assert deps._ASCII_MONTH is datetypes.MONTH_SHAPE


# ------------------------------------------------------------ ② 五个端点


def test_五个端点都拒绝不存在的月份(client, admin):
    """body 形态三处（基金预结 / 财务记账 / 薪酬）+ 查询参数两处（月报导出 / 质控统计）。

    body 校验先于业务查找，所以 pool_id / org_id / employee_id 填不存在的也能证明
    422 来自 period；反过来给合法期间时同一请求必须**越过**校验（404 或 200），
    证明换上真源没把合法输入也挡掉。
    """
    bad, good = "2026-13", "2026-12"

    def post(path, body):
        return client.post(path, json=body, headers=admin)

    cases = [
        ("POST", "/api/fund/pools/999999/periods", {"period": bad}, {"period": good}),
        ("POST", "/api/mgmt/finance",
         {"org_id": 999999, "period": bad, "category": "income", "amount": 1},
         {"org_id": 999999, "period": good, "category": "income", "amount": 1}),
        ("POST", "/api/mgmt/payroll",
         {"employee_id": 999999, "period": bad, "base_salary": 1},
         {"employee_id": 999999, "period": good, "base_salary": 1}),
    ]
    for _method, path, bad_body, good_body in cases:
        rejected = post(path, bad_body)
        assert rejected.status_code == 422, (path, rejected.text)
        assert "2026-13" in rejected.text and "不存在" in rejected.text, (path, rejected.text)
        passed = post(path, good_body)
        assert passed.status_code != 422, (path, passed.text)  # 合法期间越过校验进入业务逻辑

    for path in ("/api/reports/operations/export", "/api/quality/records/qc-summary"):
        rejected = client.get(path, params={"period": bad}, headers=admin)
        assert rejected.status_code == 422, (path, rejected.text)
        assert rejected.json() == {"detail": "period 2026-13 不存在（月份须为 01~12）"}, path
        shape = client.get(path, params={"period": "2026-1"}, headers=admin)
        assert shape.json() == {"detail": "period 格式须为 YYYY-MM"}, path  # 原文案不变
        passed = client.get(path, params={"period": good}, headers=admin)
        assert passed.status_code == 200, (path, passed.text)



@pytest.mark.parametrize("path", ["/api/mgmt/finance/summary", "/api/mgmt/payroll"])
def test_月度查询参数写错一律422_留空照旧(client, admin, path):
    """P1-62：这两个 `period` 原是裸 `str`，从来不在上面那五个端点的名单里——
    `period=abc` 200，财务汇总的回执原样写着 `"period": "abc"`、合计为零。
    页面调它们不带期间，纯对接方入口；留空仍等于不筛。"""
    for bad, detail in (
        ("2026-13", "period 2026-13 不存在（月份须为 01~12）"),
        ("abc", "period 格式须为 YYYY-MM"),
        ("2026-9", "period 格式须为 YYYY-MM"),
    ):
        resp = client.get(path, params={"period": bad}, headers=admin)
        assert resp.status_code == 422, (path, bad, resp.text)
        assert resp.json() == {"detail": detail}, (path, bad)
    base = client.get(path, headers=admin)
    blank = client.get(path, params={"period": ""}, headers=admin)
    assert base.status_code == blank.status_code == 200, (base.text, blank.text)
    assert base.json() == blank.json()
    assert client.get(path, params={"period": "2026-12"}, headers=admin).status_code == 200


# ------------------------------------------------------------ ③ 单一真源


def test_覆盖面自证():
    files = _source_files()
    print(
        f"\n[月度期间单一真源守卫] 真源 {datetypes.MONTH_SHAPE.pattern!r}；"
        f"扫描 {len(files)} 个 .py（app/ 全量，除真源文件本身）；"
        f"违规 {len(_offenders())} 处"
    )
    assert files, "扫描范围为空 = 这道闸门什么也没守"


def test_月度正则不得在别处重写():
    offenders = _offenders()
    assert offenders == [], (
        "以下位置又写了一遍月度期间的正则——正则只管形状不管日历，`2026-13` 会照样入库/照样"
        "变成一份全空的报表：\n  " + "\n  ".join(offenders)
        + "\n\nbody 字段请改用 datetypes.PeriodStr，查询参数请用 deps.require_month。"
    )


# ------------------------------------------------------------ ④ 棘轮：月度期间查询参数（P1-62）
#
# ③ 盯的是"别处不许再写月度正则"——从来没写过正则、只是裸 `str` 的 `period` 查询参数
# 不在它的视野里（P1-58 在日期那头遇到的是同一个盲区）。2026-09-24 扫出 12 处，逐个核过、
# 实测、收口；这条守着别再长出来。
#
# 判据推导：路由函数里名为 period / month（或以 _period / _month 结尾）的裸 `str` 参数，
# 看它有没有交给守卫——**跟进一层本模块 helper**：`analytics.performance_report` 把
# `period` 交给 `build_variable_index`，后者再交给 `month_bounds`；只看函数体会把它误报。

MONTH_GUARDS = frozenset(
    {"require_month", "month_bounds", "period_bounds", "check_assess_period", "_period_range"}
)

#: 名字像期间、按设计却不是 `YYYY-MM` 的查询参数。**只许变少**，每条写理由。
NOT_A_MONTH: dict[str, str] = {
    "spd/routers/assess.py::list_scores::period":
        "按考核期标签等值筛；修复前写进库的垃圾期（如 2026-8）要能按原标签查出来处置",
    "spd/routers/assess.py::score_analysis::period": "同上：考核期标签，不是查询窗口",
    "spd/routers/followup.py::list_report_templates::period":
        "报表模板的推送周期枚举（daily/weekly/monthly/custom），不是期间",
    "spd/routers/workbench.py::region_stats::period":
        "声明了却从不读，口径待裁定（docs/待裁定事项清单.md 的 P1-62 那条）",
}

#: 未经校验的 `YYYY-MM` 查询参数（P1-62）。2026-09-24 清零；空集合照样是棘轮。
KNOWN_BARE_MONTH_PARAMS: set[str] = set()

_HTTP_VERBS = ("get", "post", "put", "patch", "delete")


def _is_month_name(name: str) -> bool:
    return name in ("period", "month") or name.endswith(("_period", "_month"))


def _directly_guarded(func) -> set[str]:
    names = set()
    for sub in ast.walk(func):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in MONTH_GUARDS:
            names.update(a.id for a in sub.args if isinstance(a, ast.Name))
    return names


def _guarding_helpers(tree) -> dict[str, set[int]]:
    """本模块里"把自己的某个形参交给守卫"的函数：{函数名: {形参下标}}。"""
    helpers: dict[str, set[int]] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            guarded = _directly_guarded(fn)
            idx = {i for i, a in enumerate(fn.args.args) if a.arg in guarded}
            if idx:
                helpers[fn.name] = idx
    return helpers


def _month_params() -> tuple[set[str], set[str]]:
    """(全部像期间的查询参数, 其中未经守卫的)。"""
    every, bare = set(), set()
    for base in (APP_DIR / "routers", APP_DIR / "spd" / "routers"):
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            helpers = _guarding_helpers(tree)
            rel = path.relative_to(APP_DIR).as_posix()
            for func in ast.walk(tree):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not any(
                    isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and d.func.attr in _HTTP_VERBS
                    for d in func.decorator_list
                ):
                    continue
                guarded = _directly_guarded(func)
                for sub in ast.walk(func):
                    if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                            and sub.func.id in helpers:
                        guarded.update(
                            sub.args[i].id for i in helpers[sub.func.id]
                            if i < len(sub.args) and isinstance(sub.args[i], ast.Name)
                        )
                for arg in list(func.args.args) + list(func.args.kwonlyargs):
                    if arg.annotation is None or not _is_month_name(arg.arg):
                        continue
                    if ast.unparse(arg.annotation) not in ("str", "str | None"):
                        continue
                    key = f"{rel}::{func.name}::{arg.arg}"
                    every.add(key)
                    if arg.arg not in guarded:
                        bare.add(key)
    return every, bare


def test_月度查询参数判据自证():
    every, bare = _month_params()
    print(f"\n[月度期间查询参数] 共 {len(every)} 处，未经守卫 {len(bare)} 处"
          f"（其中按设计不是 YYYY-MM 的豁免 {len(NOT_A_MONTH)} 处）")
    assert len(every) >= 15, "数到的期间参数太少，扫描面可能不对"
    # 跟进一层 helper 真的生效：performance_report 经 build_variable_index → month_bounds
    assert "routers/analytics.py::performance_report::period" in every
    assert "routers/analytics.py::performance_report::period" not in bare


def test_不得新增未经校验的月度查询参数():
    new = sorted(_month_params()[1] - NOT_A_MONTH.keys() - KNOWN_BARE_MONTH_PARAMS)
    assert new == [], (
        "以下月度期间查询参数是裸 `str`、没经过任何月度校验——`2026-9`、`abc` 会得到 200 的空表：\n  "
        + "\n  ".join(new)
        + "\n\n用 deps.require_month（只校验）或 deps.month_bounds（要区间）；"
        "若它按设计就不是 YYYY-MM（标签/枚举），进 NOT_A_MONTH 并写明理由。"
    )


def test_月度查询参数名单与豁免都只许变少():
    every, bare = _month_params()
    stale = sorted(KNOWN_BARE_MONTH_PARAMS - bare)
    assert stale == [], "这些已经接上校验了，请从 KNOWN_BARE_MONTH_PARAMS 划掉：\n  " + "\n  ".join(stale)
    gone = sorted(k for k in NOT_A_MONTH if k not in bare)
    assert gone == [], (
        "这些豁免已经不成立（参数没了，或已经接上校验）——请从 NOT_A_MONTH 划掉：\n  "
        + "\n  ".join(gone)
    )
