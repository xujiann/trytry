"""入参诚实性守卫：声明了的参数必须真的被用。

## 为什么要有这一条（P2-12）

`GET /api/spd/stats/region` 的签名里有个 `period: str = ""`，而函数体**一次都
没读它**。后果不是报错，是**静默骗人**：调用方传 `period=2026-09` 想看那个月的
结构，拿到的是全量，而且响应 200、没有任何提示。

这比死代码坏一档。死代码只是没人走到；受理即丢的参数是**声明了一个不存在的
能力**——它出现在 OpenAPI 里、出现在接口文档里、前端会照着写，而它什么都不做。

实测全仓库只有这一处（948 个端点扫一遍），所以基线是 **0**：不是"欠账很少"，
而是"这类缺陷现在一个都没有，出现第一个就当场变红"。

## 判据

对每个路由处理函数：签名里的参数名，如果在函数体的 AST 里既不作为 `Name`
出现、也不作为属性名出现，就是"受理即丢"。

排除两类，理由都写在 `_EXEMPT_NAMES` 与代码里：

* **框架注入**（`db` / `response` / `request` / `user` / `account`）：它们靠
  `Depends(...)` 或类型注解起作用，"没在函数体里被读"是正常的——
  `response: Response` 只被 FastAPI 用来收响应头，`Depends(require_roles(...))`
  的守卫在依赖求值时就跑完了。
* **`Depends(...)` 默认值的参数**：同上，作用发生在函数体之外。

## 认不出的形态（如实声明）

1. **只在 f-string 里用到的参数**——`ast.walk` 能看见 f-string 里的 `Name`，
   所以这类是认得出的；写在这里是因为它容易被误以为是盲区。
2. **通过 `locals()` / `**kwargs` 间接使用**：认不出，会误报。全仓库无此写法，
   真出现了应当在这里加豁免并写明。
3. 参数名与某个**同名属性**撞车时会被误判成"用到了"（漏报）。
   例如参数 `status` 与 `row.status` 同名——本守卫因此对这类参数偏宽松。
   宁可漏报不误报：一条假红会让人来改守卫而不是改代码。
"""
from __future__ import annotations

import ast
import inspect

from test_api_contract_governance import _iter_endpoints

#: 框架注入的参数：作用发生在函数体之外，"没被读"是正常的。
_EXEMPT_NAMES = frozenset({"db", "response", "request", "user", "account", "self"})

#: 受理即丢的参数数。**只允许调小，现在是 0。**
#: 轨迹：1（`spd/workbench:region_stats` 的 `period`）→ **0**。
BASELINE_DEAD_PARAMS = 0


def _dead_params() -> list[str]:
    out: list[str] = []
    for mod, route in _iter_endpoints():
        try:
            src = inspect.getsource(route.endpoint)
            tree = ast.parse(src.lstrip())
            sig = inspect.signature(route.endpoint)
        except (OSError, TypeError, SyntaxError):  # pragma: no cover
            continue
        fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
            None,
        )
        if fn is None:  # pragma: no cover
            continue
        used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        used |= {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        method = sorted(route.methods - {"HEAD", "OPTIONS"})[0]
        for name, prm in sig.parameters.items():
            if name in _EXEMPT_NAMES:
                continue
            if repr(prm.default).startswith("Depends"):
                continue
            if name not in used:
                out.append(f"{mod} {method} {route.path} 参数 `{name}`")
    return sorted(out)


def test_没有受理即丢的参数():
    """声明了却从不读取的参数 = 声明了一个不存在的能力，静默骗调用方。"""
    dead = _dead_params()
    assert len(dead) <= BASELINE_DEAD_PARAMS, (
        f"出现了 {len(dead)} 处受理即丢的参数（基线 {BASELINE_DEAD_PARAMS}）：{dead}。"
        " 要么实现它，要么把它从签名里删掉——留着它，OpenAPI 与接口文档会对外"
        "声明一个不存在的能力，而调用方传了不会有任何报错。"
    )


def test_基线保持收紧不放水():
    assert len(_dead_params()) == BASELINE_DEAD_PARAMS, (
        "实测与基线不一致：清完请把 BASELINE_DEAD_PARAMS 调成实测值。"
    )


def test_覆盖面自证(capsys):
    """打印分母与豁免，让覆盖面可核对而不是靠相信。"""
    total = sum(1 for _ in _iter_endpoints())
    checked = 0
    for _mod, route in _iter_endpoints():
        try:
            sig = inspect.signature(route.endpoint)
        except (TypeError, ValueError):  # pragma: no cover
            continue
        checked += sum(
            1
            for n, p in sig.parameters.items()
            if n not in _EXEMPT_NAMES and not repr(p.default).startswith("Depends")
        )
    with capsys.disabled():
        print(f"\n  分母：{total} 个端点、其中被判定的业务参数 {checked} 个"
              "（扣掉框架注入与 Depends 注入）")
        print(f"  受理即丢：{len(_dead_params())}（基线 {BASELINE_DEAD_PARAMS}）")
        print("  声明的盲区：`locals()`/`**kwargs` 间接使用认不出（全仓库无此写法）；"
              "参数名与同名属性撞车会漏报（宁可漏报不误报）")
    assert checked > 0
