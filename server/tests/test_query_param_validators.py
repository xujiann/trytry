"""闸门：带校验器的类型别名不许写成 `参数: 别名 = Query(...)`——那样 FastAPI 会把校验器丢掉（P1-88）。

`datetypes` 里的 `DateStr` / `OptionalDateStr` / `PeriodStr` / `OptionalPeriodStr` / `TimeStr` 都是
`Annotated[str, BeforeValidator(...)]`。放在请求体模型里一切正常；可作为查询参数写成

    start_date: OptionalDateStr = Query(default="")

时，FastAPI（0.141）用默认值里的 `Query()` 重建字段，**Annotated 里的校验器被丢掉**——参数照收，
`2026-02-31`、`2026/09/30` 原样进筛选条件。只有 `Annotated[别名, Query()] = 默认值` 的写法保得住
（下面的自证用例拿一个最小应用把两种写法都跑一遍，钉住这个前提）。

2026-09-24 实测：仓库里 7 个日期查询参数是前一种写法（传染病症候群 / 病原监测的起止日期、
疫苗接种统计的起止日期、手术间撮合的日期），校验形同虚设；日期查询参数的棘轮
（`test_date_query_params.py`）只看注解为裸 `str` 的参数，见到别名就当已校验，也没看出来。
7 处都改回仓库查询参数的约定（`str` + `deps.require_date`），由那道棘轮接着盯。

零基线、零豁免：见到这种写法就红。
"""
# 不用 `from __future__ import annotations`：下面的自证用例在函数里现建 FastAPI 端点，
# 注解变成字符串之后 FastAPI 解析不了局部作用域里的 `typing.Annotated[...]`
import ast
import pathlib
import typing

import pytest

from app import datetypes

#: 零基线：一条都不许有（`scripts/dump_gate_status.py` 把它列进闸门现状）
BASELINE = 0

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"
_HTTP_VERBS = ("get", "post", "put", "patch", "delete")
_PARAM_FACTORIES = {"Query", "Path", "Header", "Cookie", "Form", "Body"}

#: `datetypes` 里全部带校验器的类型别名（从模块推导，新增别名自动纳入）
VALIDATED_ALIASES = frozenset(
    name for name, value in vars(datetypes).items()
    if typing.get_origin(value) is typing.Annotated
)


def _route_functions(sources: dict[str, str] | None = None):
    files = {str(p.relative_to(APP_DIR)): p.read_text(encoding="utf-8")
             for base in (APP_DIR / "routers", APP_DIR / "spd" / "routers")
             for p in sorted(base.rglob("*.py")) if "__pycache__" not in p.parts}
    files.update(sources or {})
    for name, text in files.items():
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in _HTTP_VERBS
                for d in node.decorator_list
            ):
                yield name, node


def _violations(sources: dict[str, str] | None = None) -> list[str]:
    found = []
    for name, fn in _route_functions(sources):
        positional = fn.args.args
        defaults = [None] * (len(positional) - len(fn.args.defaults)) + list(fn.args.defaults)
        pairs = list(zip(positional, defaults)) + list(zip(fn.args.kwonlyargs, fn.args.kw_defaults))
        for arg, default in pairs:
            if arg.annotation is None or default is None:
                continue
            alias = ast.unparse(arg.annotation).replace(" | None", "")
            if alias not in VALIDATED_ALIASES:
                continue
            if isinstance(default, ast.Call) and ast.unparse(default.func) in _PARAM_FACTORIES:
                found.append(f"{name}:{fn.name}:{arg.arg}")
    return sorted(found)


def test_别名清单推导得出来():
    assert {"DateStr", "OptionalDateStr", "PeriodStr", "OptionalPeriodStr", "TimeStr"} <= VALIDATED_ALIASES


def test_带校验器的别名不许写成_别名_等于_Query():
    bad = _violations()
    assert len(bad) <= BASELINE, (
        "以下参数写成了 `参数: 别名 = Query(...)`，FastAPI 会丢掉别名里的校验器：\n  " + "\n  ".join(bad)
        + "\n\n日期查询参数照仓库约定写 `x: str = \"\"` + `deps.require_date`；"
        "其余写成 `x: Annotated[别名, Query()] = 默认值`。"
    )


def test_判据自证_修复前的写法当场点名():
    snippet = (
        "@router.get('/x')\n"
        "def f(start_date: OptionalDateStr = Query(default=''), ok: Annotated[TimeStr, Query()] = '08:00'):\n"
        "    pass\n"
    )
    assert [v for v in _violations({"自证.py": snippet}) if v.startswith("自证.py")] == ["自证.py:f:start_date"]


@pytest.fixture(scope="module")
def probe_app():
    from fastapi import FastAPI, Query
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/dropped")
    def dropped(d: datetypes.OptionalDateStr = Query(default="")):  # noqa: B008 —— 自证要的正是这种写法
        return {"d": d}

    @app.get("/kept")
    def kept(d: typing.Annotated[datetypes.OptionalDateStr, Query()] = ""):
        return {"d": d}

    return TestClient(app)


def test_前提自证_别名等于Query的写法校验器真的被丢掉(probe_app):
    """若哪天 FastAPI 修了这一点，这条会红——那时这道闸门可以降级为风格约定，请连同 docstring 一起改。"""
    assert probe_app.get("/dropped", params={"d": "2026-02-31"}).status_code == 200
    assert probe_app.get("/kept", params={"d": "2026-02-31"}).status_code == 422
    assert probe_app.get("/kept", params={"d": "2026-02-28"}).status_code == 200


# ---------------------------------------------------------------- 七个参数改回约定写法之后，真的校验了
ENDPOINTS = [
    ("/api/surveillance/syndromes", "start_date", {}),
    ("/api/surveillance/syndromes", "end_date", {}),
    ("/api/surveillance/pathogens", "start_date", {}),
    ("/api/surveillance/pathogens", "end_date", {}),
    ("/api/vaccine-supply/stats", "start_date", {}),
    ("/api/vaccine-supply/stats", "end_date", {}),
    ("/api/resources/match/or-rooms", "scheduled_date", {"org_id": 1}),
]


@pytest.mark.parametrize("path,param,extra", ENDPOINTS)
@pytest.mark.parametrize("bad", ["2026-02-31", "2026/09/30"])
def test_写坏的日期_422而不是原样进筛选条件(client, admin, path, param, extra, bad):
    r = client.get(path, params={param: bad, **extra}, headers=admin)
    assert r.status_code == 422, (path, param, bad, r.status_code, r.text[:200])
    assert param in r.text


@pytest.mark.parametrize("path,param,extra", ENDPOINTS)
def test_特征化_合法日期与不填照常(client, admin, path, param, extra):
    assert client.get(path, params={param: "2026-02-28", **extra}, headers=admin).status_code == 200
    assert client.get(path, params=extra, headers=admin).status_code == 200
