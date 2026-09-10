"""`async def` 端点必须在扫路由的 AST 闸门分母里。

## 防的是哪一种失效

`ast.walk()` 走出来的 `async def` 是 **`ast.AsyncFunctionDef`**，它**不是**
`ast.FunctionDef` 的子类。所以下面这句在本仓库里非常常见的写法——

    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:

会把每一个 `async def` 端点**整个跳过**：规则没被删、豁免清单没变长、
闸门照样报绿，只是那些端点**从此不在任何分母里**。

这与 `test_stage15_horizontal.py::test_路由扫描必须递归到子包` 防的是同一种
失效的另一副面孔：那次是**换个目录**（路由拆进子包，一层扫漏掉整包），
这次是**换个关键字**（`def` 换成 `async def`）。两次都不报错、都不变红。

## 为什么现在补

2026-09-10 实测：全仓 946 个端点函数里有 3 个是 `async def`，而
`test_stage14_concurrency` / `test_stage15_horizontal` / 分页棘轮 /
排序稳定性四个闸门共 11 处枚举点全都只写了 `ast.FunctionDef`。
逐条核过，那 3 个端点**当前代码都是对的**（附件上传有
`assert_owner_visible(..., write=True)`；支付回调按设计免登录、由 HMAC 验签
承担身份，且不直接 `db.add`；spd 佐证上传在居民端体系内）——所以放宽分母是
**零基线**（0 处新命中、0 条新豁免）。补的不是今天的洞，是**明天的**：
下一个 `async def` 端点写错了，今天这些闸门一条都不会红。
"""
import ast
import os

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROUTER_DIRS = (
    os.path.join(TESTS_DIR, "..", "app", "routers"),
    os.path.join(TESTS_DIR, "..", "app", "spd", "routers"),
)
HTTP_METHODS = (".get(", ".post(", ".put(", ".patch(", ".delete(")


def _router_files() -> list[str]:
    files = []
    for directory in ROUTER_DIRS:
        for root, dirs, names in os.walk(os.path.abspath(directory)):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            files += [os.path.join(root, n) for n in sorted(names) if n.endswith(".py")]
    return sorted(files)


def _endpoints() -> tuple[list[str], list[str]]:
    """(同步端点, async 端点)，都用 `函数名@相对路径` 表示。"""
    sync_eps, async_eps = [], []
    for path in _router_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        rel = os.path.relpath(path, os.path.join(TESTS_DIR, ".."))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decs = " ".join(ast.unparse(d) for d in node.decorator_list)
            if not any(m in decs for m in HTTP_METHODS):
                continue
            (async_eps if isinstance(node, ast.AsyncFunctionDef) else sync_eps).append(
                f"{node.name}@{rel}"
            )
    return sync_eps, async_eps


def test_本仓库确实存在async端点():
    """防空转：没有 async 端点时，下面那条规则等于什么都没守。

    这条不写死条数——写死会在第 4 个 async 端点加进来时变成"改个数字就绿"。
    钉的是"这条规则不是空转"，并把当前清单打印出来，好让人一眼看见分母长什么样。
    """
    sync_eps, async_eps = _endpoints()
    print(
        f"\n[async 端点] 全仓端点 {len(sync_eps) + len(async_eps)} 个，"
        f"其中 async {len(async_eps)} 个：" + "、".join(sorted(async_eps))
    )
    assert async_eps, (
        "全仓一个 async 端点都没有——下面那条规则从此空转。"
        "若确属有意（全部端点改回同步），请连同这两条用例一起删掉并写明理由，"
        "不要留一条不会失败的规则在这里充数。"
    )
    assert sync_eps, "一个同步端点都没扫到，_endpoints() 大概已经坏了"


def test_扫路由的闸门必须把async端点算进分母():
    """凡是扫路由文件的测试模块，`isinstance(x, ast.FunctionDef)` 必须同时认 async。

    **基线为空**：一处都不许只写 `ast.FunctionDef`。

    判据只看 `isinstance(...)` 调用，不看类型注解（`def f(fn: ast.FunctionDef)`
    只是形参声明，不决定谁进分母）。作用域限定在"引用了路由目录"的测试模块——
    别的模块扫的是迁移、模型、前端，与本规则无关。
    """
    offenders = []
    scanned_modules, isinstance_sites = set(), 0
    for name in sorted(os.listdir(TESTS_DIR)):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        path = os.path.join(TESTS_DIR, name)
        src = open(path, encoding="utf-8").read()
        if "app/routers" not in src and "_router_files()" not in src and '"routers"' not in src:
            continue
        scanned_modules.add(name)
        for node in ast.walk(ast.parse(src)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "isinstance" or len(node.args) != 2:
                continue
            checked = ast.unparse(node.args[1])
            if "ast.FunctionDef" not in checked:
                continue
            isinstance_sites += 1
            if "ast.AsyncFunctionDef" not in checked:
                offenders.append(f"{name}:{node.lineno}  isinstance(..., {checked})")

    assert scanned_modules, "一个扫路由的测试模块都没找到——本规则的作用域判据坏了"
    assert isinstance_sites, (
        "扫路由的模块里一处 `isinstance(..., ast.FunctionDef)` 都没有——"
        "要么写法变了、要么作用域判据坏了，两种都得来看一眼，别让它静默通过"
    )
    assert offenders == [], (
        "以下位置只认 `ast.FunctionDef`，`async def` 端点会被整个跳过"
        "（闸门照样报绿，只是那些端点不在分母里）：\n  "
        + "\n  ".join(offenders)
        + "\n改成 `isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))`。"
    )
