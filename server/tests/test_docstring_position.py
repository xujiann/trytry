"""函数 docstring 必须是函数体的**第一条**语句，否则它只是一个没人读的字符串表达式（P2-4）。

盘点时 11 个端点长这样：

    def create_voucher(body, db, user):
        assert_org_writable(db, user, body.org_id)
        \"\"\"录入凭证（草稿）。借贷不平直接 422……\"\"\"

——上一轮补横向越权校验时把 `assert_org_writable` 插到了 docstring 上面。Python 不报错，
但 `__doc__` 从此是 None：FastAPI 生成的 OpenAPI 里这 11 个端点没有 description，
接口文档页上是一段空白，而写下那句口径的人以为自己已经写在文档里了。

推导而非枚举：扫 `app/` 全部函数，"第一条不是 docstring、后面却跟着一条字符串表达式"
即违规。修法只有一种——把字符串挪回首行。
"""
from __future__ import annotations

import ast
import importlib
import pathlib
import pkgutil

from fastapi import APIRouter
from fastapi.routing import APIRoute

import app.routers as platform_routers
import app.spd.routers as spd_routers
from app.main import app

SERVER_DIR = pathlib.Path(__file__).resolve().parents[1]
APP_DIR = SERVER_DIR / "app"


def _iter_routes():
    """两个路由包（含子包）里的全部 APIRoute，按对象去重（config 子包多处 import 同一个 router）。

    不用 `app.routes`：那上面挂的是被中间件/子应用包过的路由对象，APIRoute 只剩个位数；
    源路由模块才是完整分母（与 test_api_contract_governance / test_privacy_egress_guard 同一口径）。
    """
    seen: set[int] = set()
    for pkg in (platform_routers, spd_routers):
        for modinfo in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
            module = importlib.import_module(modinfo.name)
            for router in (v for v in vars(module).values() if isinstance(v, APIRouter)):
                for route in router.routes:
                    if isinstance(route, APIRoute) and id(route) not in seen:
                        seen.add(id(route))
                        yield route


def _is_string_stmt(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _misplaced_docstrings() -> list[str]:
    out = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            body = fn.body
            if not body or _is_string_stmt(body[0]):
                continue
            stray = next((s for s in body[1:] if _is_string_stmt(s)), None)
            if stray is not None:
                out.append(f"{path.relative_to(SERVER_DIR)}:{stray.lineno} {fn.name}")
    return out


def test_docstring不得被顶到第二条语句():
    offenders = _misplaced_docstrings()
    assert offenders == [], (
        "以下函数的 docstring 不在函数体首行（前面插了别的语句），`__doc__` 为 None、"
        "OpenAPI 里没有 description：\n  " + "\n  ".join(offenders)
        + "\n把字符串挪回函数体第一行即可。"
    )


def test_端点写了docstring的_OpenAPI就有description():
    """闭环到产物：凡端点函数带 docstring，OpenAPI 里该操作必须带 description。

    上面那条盯源码形状，这条盯生成物——FastAPI 从 `__doc__` 取 description，
    形状对了产物就该在；产物不在，说明取法变了或 docstring 又被顶下去了。
    """
    schema = app.openapi()
    checked = 0
    missing = []
    for route in _iter_routes():
        if not route.include_in_schema or not (route.endpoint.__doc__ or "").strip():
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            operation = schema["paths"].get(route.path, {}).get(method.lower())
            if operation is None:
                continue
            checked += 1
            if not operation.get("description"):
                missing.append(f"{method} {route.path}")
    print(f"\n[docstring→OpenAPI] 带 docstring 的端点操作 {checked} 个，缺 description {len(missing)} 个")
    assert checked > 100, "分母异常地小——路由枚举口径变了，这条什么也没守"
    assert missing == [], "端点函数有 docstring 但 OpenAPI 无 description：\n  " + "\n  ".join(missing)
