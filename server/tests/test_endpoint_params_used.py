"""端点声明了、函数体里一次都没用到的参数（P2-622）。

这类参数是**悄悄不生效的筛选**：页面照样把「期间」「状态」「机构」送过来，接口照收、照 200，结果与没送一模一样——
用户以为筛过了。它不报错、不变红、也不写日志，只有对着数一条条核才看得出来。2026-09-27 全仓实测只有一处，
且是明知的（`region_stats` 的 `period`，docstring 写明「目前不生效」、口径等 P1-62 裁定），于是立成零基线：
新加的端点参数没用上即红；按设计留着不用的，登记进 `ALLOWED_UNUSED` 并写明理由。

判据：路由装饰器（`router.` 开头）下的函数，形参里去掉依赖注入（`Depends(` / `Security(`）与框架对象
（`db` / `user` / `response` / `request` / `background_tasks`），剩下的名字在函数体（含内层函数）里一次都没出现。
"""
import ast
import os
import pathlib

SERVER = pathlib.Path(__file__).resolve().parents[1]

#: 框架注入、不是调用方送来的参数
_FRAMEWORK = {"db", "user", "response", "request", "background_tasks", "self"}

#: 【按设计不用，逐条写明理由；只减不增】`文件:端点:参数`
ALLOWED_UNUSED: dict[str, str] = {
    "spd/workbench.py:region_stats:period": "区域结构分析是当前在管档案的结构快照，按哪个期间看待裁定（P1-62）；"
                                            "参数先收着、docstring 写明「目前不生效」，裁定后接上",
}


def _router_files() -> list[tuple[str, str]]:
    out = []
    for base, label in (("app/routers", ""), ("app/spd/routers", "spd/")):
        root = os.path.abspath(SERVER / base)
        for d, dirs, names in os.walk(root):
            dirs[:] = sorted(x for x in dirs if x != "__pycache__")
            rel = os.path.relpath(d, root)
            pre = "" if rel == "." else rel.replace(os.sep, "/") + "/"
            for n in sorted(x for x in names if x.endswith(".py")):
                out.append((f"{label}{pre}{n}", os.path.join(d, n)))
    return sorted(out)


def unused_in(name: str, tree: ast.Module) -> set[str]:
    """一个路由文件里，端点声明了却没用到的参数：{`文件:端点:参数`}。"""
    found = set()
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        if not any(ast.unparse(d).startswith("router.") for d in fn.decorator_list):
            continue
        args = fn.args.args + fn.args.kwonlyargs
        defaults = ([None] * (len(fn.args.args) - len(fn.args.defaults)) + list(fn.args.defaults)
                    + list(fn.args.kw_defaults))
        used = {n.id for stmt in fn.body for n in ast.walk(stmt) if isinstance(n, ast.Name)}
        for arg, default in zip(args, defaults):
            src = ast.unparse(default) if default is not None else ""
            if arg.arg in _FRAMEWORK or "Depends(" in src or "Security(" in src:
                continue
            if arg.arg not in used:
                found.add(f"{name}:{fn.name}:{arg.arg}")
    return found


def unused_params() -> set[str]:
    found: set[str] = set()
    for name, path in _router_files():
        found |= unused_in(name, ast.parse(open(path, encoding="utf-8").read()))
    return found


def test_端点参数都用上了():
    found = unused_params()
    new = sorted(found - set(ALLOWED_UNUSED))
    assert new == [], (
        "以下端点参数声明了、函数体里一次都没用到——页面送了、接口照收照 200，结果与没送一样（悄悄不生效的筛选）：\n  "
        + "\n  ".join(new)
        + "\n接上它，或删掉它；按设计先收着不用的，登记进 ALLOWED_UNUSED 并写明理由。"
    )
    stale = sorted(set(ALLOWED_UNUSED) - found)
    assert stale == [], f"这些登记项已经用上了（或端点改名 / 删除），应从 ALLOWED_UNUSED 删掉：{stale}"
    blank = sorted(k for k, why in ALLOWED_UNUSED.items() if len(why.strip()) < 12)
    assert blank == [], f"登记必须写明理由：{blank}"


def test_判据自证():
    """用了的（含只在内层函数里用）、依赖注入的、框架对象不报；声明了不用的报；非路由函数不管。"""
    sample = ast.parse(
        "@router.get('/a')\n"
        "def listing(status: str = '', period: str = '', org_id: int | None = None,\n"
        "            db: Session = Depends(get_db), me: User = Depends(get_current_user)):\n"
        "    def inner():\n"
        "        return org_id\n"
        "    return q.filter(x == status), inner()\n"
        "def helper(unused_arg):\n"
        "    return 1\n"
    )
    assert unused_in("sample.py", sample) == {"sample.py:listing:period"}
    assert unused_params() >= set(ALLOWED_UNUSED), "前提没了：登记的那一处应当仍被认出"
