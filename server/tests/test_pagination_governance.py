"""分页治理——棘轮测试（ratchet）。

    列表端点：要么能翻页、要么至少说得出「一共多少条」，
    绝不能**悄悄只给前 N 条**。

## 为什么要有这一条（P2-8）

`docs/TECH_DEBT.md` 的 P2-8 写着「paginate 仅 32/89 文件；210 处直接 `.limit()`
会随数据量静默截断」——**但一直没有任何闸门**。于是它只会越欠越多：新写一个
`return [...]` 加个 `.limit(200)` 谁都不会觉得有问题，而它的后果要等到县域平台
跑满一年、某张表过两百行那天才出现，表现是**界面上少了记录而没有任何提示**。

这类缺陷有个共同点：**开发库上永远看不出来**。种子数据几十行，`.limit(200)`
和不限一模一样。所以只能靠静态判定，不能靠"跑一下看看"。

## 判据（三个桶，全部从路由与源码推导，不手写清单）

分母：`response_model` 是 `list[...]` 的 GET 端点——**它承诺自己返回一个清单**。

| 桶 | 判据 | 问题 |
|---|---|---|
| **已分页** | 处理函数调用 `deps.paginate` | 可翻页 + `X-Total-Count`，达标 |
| **半分页** | 有 `offset`/`limit` 入参但不走 `paginate` | 能调上限，但**不知道总数** |
| **静默截断** | 有字面量 `.limit(N)`，无分页入参 | 只给前 N 条，**调用方无从知道** |
| **无上限** | 连 `.limit()` 都没有 | 全表出网，数据长起来就是一次 OOM |

后三桶合计即欠账，**只许调小**。

## 怎么还这笔账（两条路，都要 byte 安全）

1. **改走 `paginate`**：把处理函数的 `limit` 入参**默认值设成它原来的硬上限**，
   于是不传参的老调用方拿到的行**一字不差**（`.limit(200)` 与
   `.offset(0).limit(200)` 同解），只是多了一个 `X-Total-Count` 响应头——
   响应头不属于 body 字节。无上限那一类不能这么改：给它加默认上限会**真的**
   截断，属行为变更，要单独判断。
2. **登记进 `BOUNDED_BY_DESIGN`**：这个清单的返回行数由**配置/目录**决定，
   不随业务量增长（角色清单、字典项、规则域……）。逐条写理由，**只减不增**，
   且必须仍然命中分母——端点改名/下线后清单没清理，
   由 `test_按设计有界清单没有陈旧条目` 拦下。

## 认不出的形态（如实声明，不假装看见）

1. **上限来自变量**（`.limit(n)`，n 是名字）——AST 只认字面量。实测 0 处，
   出现了会进"无上限"桶（宁可误报也不漏报），并由 `test_覆盖面自证` 打印。
2. **嵌套在对象里的列表**：`response_model` 不是 `list[...]`、但响应体某个字段
   是长列表的端点（统计页的 `items`/`rows`/`logs`）不在分母里。实测另有 32 个
   GET 端点带字面量 `.limit()` 却不在分母——它们多数是**按设计的 TOP N**
   （近三次测量、TOP 20 教材），把它们一并算进欠账会掺进永远还不掉的账。
   数量由 `test_覆盖面自证` 打印，**让它可见而不是悄悄绕过**。
3. 非 GET 端点不在分母：写操作不返回长清单。
"""
from __future__ import annotations

import ast
import inspect

from fastapi import APIRouter
from fastapi.routing import APIRoute

import app.routers as platform_routers
import app.spd.routers as spd_routers

# 复用契约棘轮的路由遍历：同一个分母来源，两条闸门不各造一份
from test_api_contract_governance import _iter_endpoints

# —— 棘轮基线 ——
#: 列表端点里**不能翻页、也说不出总数**的个数（静默截断 + 无上限 + 半分页）。
#: **只允许调小。** 轨迹：214（本闸门建成时的实测）。
BASELINE_UNPAGED = 214

#: 按设计有界：返回行数由**配置/目录**决定，不随业务量增长。
#: 逐条写理由；只减不增；条目必须仍然命中分母。
BOUNDED_BY_DESIGN: dict[str, str] = {}


def _bucket(route: APIRoute) -> str:
    """把一个列表端点归进四个桶之一。判定只看签名与源码，不看运行期。"""
    try:
        src = inspect.getsource(route.endpoint)
        tree = ast.parse(src.lstrip())
        params = set(inspect.signature(route.endpoint).parameters)
    except (OSError, TypeError, SyntaxError):  # pragma: no cover - 取不到源码
        return "无源码"
    if "paginate(" in src:
        return "已分页"
    if {"offset", "limit"} & params:
        return "半分页"
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "limit"
            and len(node.args) == 1
        ):
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
                return "静默截断"
            # 上限来自变量：认不出到底有没有上限，按最坏算（见模块 docstring 盲区 1）
            return "无上限"
    return "无上限"


def _key(mod: str, route: APIRoute) -> str:
    method = sorted(route.methods - {"HEAD", "OPTIONS"})[0]
    return f"{mod} {method} {route.path}"


def _survey() -> tuple[dict[str, list[str]], int]:
    """分母内的四桶归类 + 分母外的"带上限的非列表端点"计数（盲区 2）。"""
    buckets: dict[str, list[str]] = {
        "已分页": [], "半分页": [], "静默截断": [], "无上限": [], "无源码": [],
    }
    outside = 0
    for mod, route in _iter_endpoints():
        if "GET" not in route.methods:
            continue
        if not str(route.response_model).startswith("list["):
            try:
                src = inspect.getsource(route.endpoint)
            except (OSError, TypeError):
                continue
            if ".limit(" in src:
                outside += 1
            continue
        buckets[_bucket(route)].append(_key(mod, route))
    return buckets, outside


def test_分页欠账不许变大():
    """列表端点里"不能翻页也说不出总数"的个数 ≤ 基线。

    新写一个 `return [...].limit(200).all()` 的列表端点 → 欠账 +1 → 变红。
    """
    buckets, _ = _survey()
    unpaged = [
        k
        for name in ("半分页", "静默截断", "无上限")
        for k in buckets[name]
        if k not in BOUNDED_BY_DESIGN
    ]
    assert len(unpaged) <= BASELINE_UNPAGED, (
        f"不能翻页也说不出总数的列表端点从基线 {BASELINE_UNPAGED} 涨到 {len(unpaged)}。"
        " 新端点请走 `deps.paginate`（`limit` 默认值取原硬上限即可保持字节不变），"
        " 或按设计有界时登记进 BOUNDED_BY_DESIGN 并写明理由。"
        f" 当前清单：{sorted(unpaged)[:20]}"
    )


def test_基线保持收紧不放水():
    """治理之后必须把基线调到实测值，不许留着虚高的额度。"""
    buckets, _ = _survey()
    unpaged = [
        k
        for name in ("半分页", "静默截断", "无上限")
        for k in buckets[name]
        if k not in BOUNDED_BY_DESIGN
    ]
    assert len(unpaged) == BASELINE_UNPAGED, (
        f"实测 {len(unpaged)} 与基线 {BASELINE_UNPAGED} 不一致——"
        "治理完请把 BASELINE_UNPAGED 调成实测值，虚高的额度等于没有闸门。"
    )


def test_按设计有界清单没有陈旧条目():
    """`BOUNDED_BY_DESIGN` 的每一条都必须仍然命中分母，且真的没分页。

    端点改名/下线/后来加了分页，条目留着就是一条假豁免——
    假豁免比没有豁免更坏：它让人以为这里被判过。
    """
    buckets, _ = _survey()
    in_denominator = {k for ks in buckets.values() for k in ks}
    still_unpaged = {
        k for name in ("半分页", "静默截断", "无上限") for k in buckets[name]
    }
    stale = sorted(k for k in BOUNDED_BY_DESIGN if k not in in_denominator)
    assert stale == [], f"这些条目已不在分母里（改名/下线？）：{stale}"
    fixed = sorted(k for k in BOUNDED_BY_DESIGN if k not in still_unpaged)
    assert fixed == [], (
        f"这些端点已经分页了，豁免该删：{fixed}"
    )


def test_每条豁免都写了理由():
    reasonless = sorted(k for k, why in BOUNDED_BY_DESIGN.items() if len(why.strip()) < 10)
    assert reasonless == [], f"这些豁免没写理由（或太短）：{reasonless}"


def test_覆盖面自证(capsys):
    """把分母、四个桶与两处盲区的实测数打印出来——闸门要自己证明覆盖面。"""
    buckets, outside = _survey()
    total = sum(len(v) for v in buckets.values())
    with capsys.disabled():
        print(f"\n  分母：`response_model` 为 list[...] 的 GET 端点 {total} 个"
              "（从路由推导，无手写清单）")
        for name in ("已分页", "半分页", "静默截断", "无上限", "无源码"):
            print(f"    {len(buckets[name]):4}  {name}")
        unpaged = sum(len(buckets[n]) for n in ("半分页", "静默截断", "无上限"))
        print(f"  欠账：{unpaged - len(BOUNDED_BY_DESIGN)}"
              f"（基线 {BASELINE_UNPAGED}；按设计有界豁免 {len(BOUNDED_BY_DESIGN)}）")
        print(f"  声明的盲区：分母外另有 {outside} 个带字面量 .limit() 的 GET 端点"
              "（响应不是 list[...]，多为统计页里按设计的 TOP N），**不算欠账**")
    assert total > 0


def test_路由遍历与契约棘轮同源():
    """两条闸门共用一份 `_iter_endpoints`——分母口径分叉会让两边的数对不上。"""
    assert _iter_endpoints.__module__ == "test_api_contract_governance"
    assert APIRouter is not None and APIRoute is not None
    assert platform_routers is not None and spd_routers is not None
