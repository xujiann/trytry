"""查询参数的条数 / 偏移量没有界就直接进 `.limit()` / `.offset()`：PG 上负数即 500，SQLite 照收（P2-54）。

2026-09-24 实测（修前代码）：审计链校验 `GET /api/audit/verify?limit=-1`、统一申请单
`GET /api/service-requests?limit=-1` 在真 PG 上都是 500（`LIMIT must not be negative`），SQLite 上 200——
统一申请单还回出 `returned: -1`；limit 超过 bigint 两个库都 500。走 `deps.paginate` 的清单不受影响
（它自己把 offset 钳到 ≥0、limit 钳到 1~500）。

修法：两个参数补 `Query(ge=0, le=INT4_MAX)`——下界挡住负数；上界只到列容量，不替管理员另设业务上限
（审计链全量校验照旧能传大数），0 照旧合法。

判据（AST，平台与慢专病两边的路由）：端点的 int 参数进 `.limit(…)` / `.offset(…)` 之前，`Query(...)`
里上下界都得有，或经 `min()` / `max()` 钳过（`paginate` 与 `offset(max(offset, 0))` 的写法）。
"""
import ast
import pathlib
import types

import pytest
from fastapi import Query

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

#: 基线：已清零，此后即零基线闸门（`scripts/dump_gate_status.py` 把它列进闸门现状）。
#: 2026-09-24 实测 2 个端点（审计链校验 1 处、统一申请单 5 处调用），同一批补完。
BASELINE = 0

_VERBS = ("get", "post", "put", "patch", "delete")


def _router_sources():
    for base in (APP_DIR / "routers", APP_DIR / "spd" / "routers"):
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" not in p.parts and p.name != "__init__.py":
                yield str(p.relative_to(APP_DIR)), p.read_text(encoding="utf-8")


def _clamps(node) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("min", "max")
               for n in ast.walk(node))


def _both_bounds(default) -> bool:
    if not (isinstance(default, ast.Call) and ast.unparse(default.func).endswith("Query")):
        return False
    keys = {k.arg for k in default.keywords}
    return bool(keys & {"ge", "gt"}) and bool(keys & {"le", "lt"})


def unbounded_limit_params(sources=None) -> list[str]:
    """`文件:端点:参数`——端点的 int 参数没有上下界、也没钳过，就进了 `.limit()` / `.offset()`。"""
    out = set()
    for rel, text in (sources if sources is not None else _router_sources()):
        for fn in ast.parse(text).body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not any(
                    isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in _VERBS
                    for d in fn.decorator_list):
                continue
            positional = fn.args.args
            defaults = dict(zip([a.arg for a in positional[len(positional) - len(fn.args.defaults):]],
                                fn.args.defaults))
            defaults.update({a.arg: d for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults) if d is not None})
            ints = {a.arg for a in positional + fn.args.kwonlyargs
                    if a.annotation is not None and ast.unparse(a.annotation) in ("int", "int | None")}
            clamped = {t.id for node in ast.walk(fn) if isinstance(node, ast.Assign) and _clamps(node.value)
                       for t in node.targets if isinstance(t, ast.Name)}
            for call in ast.walk(fn):
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr in ("limit", "offset") and call.args) or _clamps(call.args[0]):
                    continue
                for n in ast.walk(call.args[0]):
                    if isinstance(n, ast.Name) and n.id in ints and n.id not in clamped \
                            and not _both_bounds(defaults.get(n.id)):
                        out.add(f"{rel}:{fn.name}:{n.id}")
    return sorted(out)


# ================================================================ 闸门
def test_条数偏移量进limit之前得有上下界():
    offenders = unbounded_limit_params()
    assert len(offenders) <= BASELINE, (
        "端点的条数 / 偏移量没有界就进了 .limit() / .offset()——PG 上负数即 500。"
        f"补 `Query(ge=…, le=…)`，或像 `deps.paginate` 那样先钳住：{offenders}"
    )


def test_判据自证_无界的点名_有界与钳过的不报():
    snippet = '''
@router.get("/a")
def loose(limit: int = 200):
    return q.limit(limit).all()

@router.get("/b")
def half(limit: int = Query(default=200, ge=0)):
    return q.limit(limit).all()

@router.get("/c")
def bounded(limit: int = Query(default=200, ge=0, le=500)):
    return q.limit(limit).all()

@router.get("/d")
def clamped(offset: int = 0, limit: int = 50):
    limit = min(max(limit, 1), 500)
    return q.offset(max(offset, 0)).limit(limit).all()

@router.get("/e")
def plus_one(limit: int = 20):
    return q.limit(limit + 1).all()

def helper(limit: int):
    return q.limit(limit).all()
'''

    class _Router:
        def __getattr__(self, _name):
            return lambda *a, **k: (lambda fn: fn)

    mod = types.ModuleType("自证")
    mod.__dict__.update({"Query": Query, "router": _Router()})
    exec(compile(snippet, "自证", "exec"), mod.__dict__)
    assert unbounded_limit_params([("自证.py", snippet)]) == [
        "自证.py:half:limit", "自证.py:loose:limit", "自证.py:plus_one:limit",
    ]


# ================================================================ 行为回归（修前实测见 docstring）
@pytest.mark.parametrize("url", ["/api/audit/verify?limit=-1", "/api/service-requests?limit=-1",
                                 "/api/service-requests?limit=99999999999999999999"])
def test_条数越界是422(client, admin, url):
    resp = client.get(url, headers=admin)
    assert resp.status_code == 422, (url, resp.status_code, resp.text[:200])


@pytest.mark.parametrize("url", ["/api/audit/verify?limit=0", "/api/audit/verify?limit=5000",
                                 "/api/service-requests?limit=0", "/api/service-requests"])
def test_界内照常(client, admin, url):
    resp = client.get(url, headers=admin)
    assert resp.status_code == 200, (url, resp.status_code, resp.text[:200])
