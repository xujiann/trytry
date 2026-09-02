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
     "２０２６-０１", "2026-1a", " 2026-01", "2026-01 "],
)
def test_真源拒绝非法或不存在的月份(period):
    with pytest.raises(ValidationError):
        _Probe(period=period)


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
