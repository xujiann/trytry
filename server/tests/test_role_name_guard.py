"""角色名只有一个真源：`deps.ROLE_NAMES`。写错一个字母，必须当场变红。

平台的内置角色名以**裸字符串**散布在 500 多处：`require_roles("doctor", …)`、
`user.role == "director"`、前端 `PAGES` 的 `roles: ["admin"]`。真源是
`app/deps.py` 的 `ROLE_NAMES`（六个内置角色），但没有任何东西核对过这些
字面量与真源一致。

**漏改一处的后果是静默的**（这正是本轮排查用的判据）：

* `require_roles("dcotor")`——`user.role == "dcotor"` 永远为假，于是这个端点
  对医师**悄悄关闭**。没有异常、没有日志；调用方拿到 403，看起来"像是权限
  配错了"，而不像是代码里有个拼写错误。
* `user.role == "publichealth"`（少个下划线）——那段公卫分支永远不执行，
  统计口径悄悄少一块。

角色名的真源在代码里已经有了，只是没人拿它去核对。这个文件把核对补上：
**清单不再重写一份，而是从 `deps.ROLE_NAMES` 推导**，扫全部 `app/` 源码里
出现的角色字面量，逐个比对。

## 覆盖面（第 17 章第 4 条：闸门要自证）

扫描认三种形态：`require_roles(...)` 的实参（含 `*常量` 展开——常量在模块级
定义时会被解析出来）、`X.role == "..."`、`X.role in (...)`。认不出的形态
（运行期拼出来的角色名、从库里读出来的自定义角色）逐项计数并打印。
自定义角色不在此列：它们**不该**出现在 `require_roles` 的字面量里，
按设计走权限点判定（见 `deps.require_roles` 的 docstring）。
"""
from __future__ import annotations

import ast
import pathlib
import warnings

from app.deps import ROLE_NAMES

SERVER_DIR = pathlib.Path(__file__).resolve().parents[1]
APP_DIR = SERVER_DIR / "app"

ROLE_CHECK_FUNCS = {"require_roles"}


def _source_files() -> list[pathlib.Path]:
    return [p for p in sorted(APP_DIR.rglob("*.py")) if "__pycache__" not in p.parts]


def _module_level_role_tuples(tree: ast.Module) -> dict[str, list[str]]:
    """模块级 ``NAME = ("doctor", …)`` 常量——`require_roles(*NAME)` 靠它解析。"""
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        value = node.value
        if not isinstance(target, ast.Name) or not isinstance(value, (ast.Tuple, ast.List)):
            continue
        items = [e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if items and len(items) == len(value.elts):
            out[target.id] = items
    return out


def _global_role_tuples(trees: dict[pathlib.Path, ast.Module]) -> dict[str, list[str]]:
    """跨模块的常量表：`CONFIG_ROLES` 定义在 `config/_base.py`、在兄弟模块里 import 使用。

    同名常量在多处定义时取并集——仓库现状是 `SERVICE_ROLES` 在四个 spd 路由里
    各写一份且取值完全相同，取并集不会放过任何一个拼错的成员。
    """
    merged: dict[str, list[str]] = {}
    for tree in trees.values():
        for name, items in _module_level_role_tuples(tree).items():
            merged[name] = sorted(set(merged.get(name, [])) | set(items))
    return merged


class _Findings:
    def __init__(self) -> None:
        self.files = 0
        self.literals: list[tuple[str, str]] = []   # (出处, 角色名)
        self.unresolved: list[str] = []             # 认不出的实参形态


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    return getattr(func, "attr", "")


def scan() -> _Findings:
    trees = {p: ast.parse(p.read_text(encoding="utf-8")) for p in _source_files()}
    globals_map = _global_role_tuples(trees)
    found = _Findings()
    for path, tree in trees.items():
        found.files += 1
        rel = path.relative_to(SERVER_DIR)
        local = _module_level_role_tuples(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in ROLE_CHECK_FUNCS:
                for arg in node.args:
                    where = f"{rel}:{node.lineno}"
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.literals.append((where, arg.value))
                    elif isinstance(arg, ast.Starred) and isinstance(arg.value, ast.Name):
                        name = arg.value.id
                        roles = local.get(name) or globals_map.get(name)
                        if roles is None:
                            found.unresolved.append(f"{where}: *{name}（常量解析不到）")
                        else:
                            found.literals += [(f"{where}(*{name})", r) for r in roles]
                    else:
                        found.unresolved.append(f"{where}: {ast.unparse(arg)[:60]}")
            # X.role == "..." / X.role in ("...", ...)
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Attribute):
                if node.left.attr != "role":
                    continue
                where = f"{rel}:{node.lineno}"
                for comparator in node.comparators:
                    if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                        found.literals.append((where, comparator.value))
                    elif isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                        for element in comparator.elts:
                            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                                found.literals.append((where, element.value))
    return found


FOUND = scan()


def test_覆盖面自证():
    distinct = sorted({r for _w, r in FOUND.literals})
    summary = "\n".join([
        "",
        "[角色名真源守卫] 覆盖面自证",
        f"  真源：app/deps.py 的 ROLE_NAMES（{len(ROLE_NAMES)} 个：{sorted(ROLE_NAMES)}），"
        "本文件不另抄一份",
        f"  扫描：{FOUND.files} 个 .py（app/ 全量，含 spd 子系统，无抽样、无跳过）",
        f"  认出的角色字面量：{len(FOUND.literals)} 处 / 去重 {len(distinct)} 个 —— {distinct}",
        f"  认不出的实参形态：{len(FOUND.unresolved)} 处"
        + (f" —— {FOUND.unresolved[:3]}" if FOUND.unresolved else "（无）"),
    ])
    print(summary)
    warnings.warn(summary, UserWarning, stacklevel=2)
    assert FOUND.literals, "一个角色字面量都没扫到 = 这道闸门什么也没守"


def test_角色字面量都在ROLE_NAMES里():
    bad = sorted({f"{where}: {role!r}" for where, role in FOUND.literals if role not in ROLE_NAMES})
    assert bad == [], (
        "以下角色名不在 deps.ROLE_NAMES 里——拼错的角色名不会报错，"
        "只会让这段守卫/分支**永远不成立**（端点对该角色悄悄关闭）：\n  "
        + "\n  ".join(bad)
        + f"\n内置角色只有：{sorted(ROLE_NAMES)}。"
        " 自定义角色不写在 require_roles 里，按设计走权限点（见 deps.require_roles）。"
    )


def test_require_roles的实参都解析得出():
    """认不出的实参 = 这道闸门看不见的角落，数量必须保持为 0。

    现状全部实参要么是字面量、要么是模块级常量展开（`*SERVICE_ROLES`），
    都解析得出。真要写出解析不出的形态（运行期拼角色名），先让这条红，
    再决定是改写法还是扩展解析——而不是让它悄悄溜进盲区。
    """
    assert FOUND.unresolved == [], (
        "以下 require_roles 实参解析不出，角色名核对覆盖不到它们：\n  "
        + "\n  ".join(FOUND.unresolved)
    )


def test_每个内置角色都真的被用到():
    """反向一条：`ROLE_NAMES` 里登记了却全仓没人用的角色，多半是清单腐烂。

    （admin 例外：`require_roles` 里 admin 永远直接放行，不需要逐处列出。）
    """
    used = {r for _w, r in FOUND.literals}
    unused = sorted(set(ROLE_NAMES) - used - {"admin"})
    assert unused == [], (
        f"ROLE_NAMES 里这些角色全仓没有任何守卫/分支用到：{unused}。"
        " 要么是角色名改了没同步，要么是这个角色已经名存实亡。"
    )
