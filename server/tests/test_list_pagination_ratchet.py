"""P2-8 的棘轮：列表接口「硬编码 `.limit(N)` 且翻不了页」的处数只减不增。

**为什么这是正确性问题而不是风格问题。** 这类端点长这样：

    return [... for x in q.order_by(...).limit(500).all()]

数据没到 500 条时一切正常；超过之后接口**静默少返回**——没有 `X-Total-Count`、
没有 `offset`，调用方（前端表格）连"还有没有"都无从知道。**列表少一半，
在页面上和"就这么多"长得一模一样**，没有任何一处会报错、变红或写日志。
`docs/接口标准与治理.md` 早就把「列表走 `deps.paginate`」写成了标准，
但一直没有守卫，于是这条标准只在新代码里被想起来。

**判据刻意收窄成"硬编码字面量 + 没有翻页参数"**，而不是"凡是 `.limit()` 都算"：

- `.limit(body.limit)` 这类调用方能自己调的不算——它至少给了调用方控制权；
- 带 `offset`/`page`/`cursor` 参数的不算——那已经能翻页了；
- 用了 `paginate()` 的不算——那正是要迁到的目标。

判据窄一点的代价是可能漏掉个别形状，好处是**报出来的每一条都是真的**——
这一轮在越权那条线上刚学过反面教训：判据比缺陷宽会淹没在假阳性里，
最后棘轮被加豁免加到失效（见 `test_stage15_horizontal.py` 里那段"负面结论"）。

**已知盲区（做变异验证时撞出来的，如实写在这儿）**：只把返回那行改回
`.limit(200)`、**签名里的 `offset` 留着不动**，这条规则看不见——因为它按
"有没有翻页参数"排除。那种状态其实比原样更糟：接口**声明了 `offset` 却忽略它**，
调用方翻页翻不动还以为是没数据。这条规则不管这个形状；真要管，得比对
"签名里有 offset" 与 "函数体里用了 offset"，属另一条规则的事。
记下来是因为：变异验证第一次没红，不是规则失效，是**我的变异造了一个规则本就
不该管的形状**——把这种"没红"当成"规则不好使"去放宽判据，才是真会出事的那步。

**判据会误报，误报率是量出来的：26 个里 3 个。** 第二批逐个人工核对 portal 两个
模块的 26 处，发现 3 处的 `.limit(N)` 其实在**嵌套子查询**上——那是刻意的业务上限，
不是分页缺陷，**照着这条规则去"迁"反而会改错语义**：

- `portal.py:portal_my_contract` —— `.limit(20)` 在"每份签约附最近 20 条履约记录"的
  子查询上；外层合同列表根本没有 limit。
- `spd/portal.py:archive` —— 30 次就诊 + 20 条随访合并成时间轴后 `[:50]`，
  30+20 恰好等于切片长度，实际永不真截断。
- `spd/portal.py:journey` —— 每个 enrollment 附最近 30 条任务 / 10 条转诊。

它们会**继续留在这个计数里**，因为规则看不出"limit 在子查询上"。不给它们建豁免名单
（那条路的终点是"守卫全绿但没人信"），改为写在这里：**这个数是上限，不是精确值**；
真要迁某一条之前，先按上面三种形状核一遍它是不是同类。

**基线是量出来的，不是估的**：170（首次量化）→ 162（切完 billing.py 8 个）
→ 139（切完 portal.py 与 spd/portal.py 23 个）。每迁一个模块就把这个数改小，只减不增。

**这条规则不管排序全序**：切分页时若排序键不唯一，OFFSET/LIMIT 会在并列行上重复+漏行
——那是拿「静默少返回」换「静默重复+漏行」，比原缺陷更糟。那件事由
`test_pagination_sort_stability.py` 独立守（零基线、零豁免），别指望这条规则替它把关。
"""
import ast
import os
import re
import warnings

#: 当前仍会静默截断的 GET 列表端点数。**只减不增**。
#: 170（2026-09-06 首次量化）→ 162（同日切完 billing.py 的 8 个）
#: → 139（2026-09-07 切完 portal.py 与 spd/portal.py 的 23 个）
BASELINE_SILENT_TRUNCATION = 139

ROUTER_DIRS = (
    (os.path.join(os.path.dirname(__file__), "..", "app", "routers"), ""),
    (os.path.join(os.path.dirname(__file__), "..", "app", "spd", "routers"), "spd/"),
)


def _router_files():
    """**必须递归**：`app/spd/routers/config/` 是子包，一层扫会整包看不见。

    同一处盲区 `test_stage14_concurrency` 与 `test_stage15_horizontal` 都修过，
    这里从一开始就按递归写，并由下面的自证用例钉住。
    """
    files = []
    for directory, label in ROUTER_DIRS:
        root_dir = os.path.abspath(directory)
        for root, dirs, names in os.walk(root_dir):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            rel = os.path.relpath(root, root_dir)
            prefix = "" if rel == "." else rel.replace(os.sep, "/") + "/"
            for name in sorted(names):
                if name.endswith(".py"):
                    files.append((f"{label}{prefix}{name}", os.path.join(root, name)))
    return sorted(files)


def silently_truncating_endpoints() -> set[str]:
    """GET 端点里「硬编码 `.limit(数字)`、没用 `paginate`、也没有翻页参数」的。"""
    found = set()
    for name, path in _router_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            decs = " ".join(ast.unparse(d) for d in fn.decorator_list)
            if ".get(" not in decs:
                continue
            body = ast.unparse(fn)
            if "paginate(" in body:
                continue
            if not re.search(r"\.limit\(\d+\)", body):
                continue
            if {"offset", "page", "cursor"} & {a.arg for a in fn.args.args}:
                continue
            found.add(f"{name}:{fn.name}")
    return found


def test_扫描必须递归到子包():
    """防的是"路由拆进子包 → 扫描静默缩水"这一种失效（与另两条闸门同一自证）。"""
    scanned = {name for name, _ in _router_files()}
    assert any(name.startswith("spd/config/") for name in scanned), (
        "没扫到 app/spd/routers/config/ 子包——_router_files() 退回成不递归了"
    )


def test_静默截断的列表端点只减不增():
    """棘轮：这个数只许变小。

    **自证覆盖面**：扫了多少文件、当前是多少，一并打印出来——
    一个不声张自己覆盖范围的绿灯，和假装看过全部的哨兵一样危险。
    """
    endpoints = silently_truncating_endpoints()
    summary = (
        f"\n[列表分页闸门] 覆盖面自证\n"
        f"  扫描文件：{len(_router_files())} 个（app/routers + app/spd/routers，递归）\n"
        f"  仍会静默截断的 GET 端点：{len(endpoints)} 个（基线 {BASELINE_SILENT_TRUNCATION}）\n"
        f"  说明：硬编码 .limit(N) 且无 offset/page/cursor——数据超过 N 就少返回，"
        f"且调用方收不到任何信号。"
    )
    print(summary)
    warnings.warn(summary, UserWarning, stacklevel=2)
    assert len(endpoints) <= BASELINE_SILENT_TRUNCATION, (
        f"静默截断的列表端点从 {BASELINE_SILENT_TRUNCATION} 涨到了 {len(endpoints)}。"
        f"新增的列表端点请走 `deps.paginate`（写法见 app/routers/billing.py）：\n  "
        + "\n  ".join(sorted(endpoints - _known())[:10])
    )


def _known() -> set[str]:
    """占位：基线是**计数**而不是名单——名单有 162 条，钉成名单只会天天冲突。

    计数棘轮的弱点是"迁走一个又新增一个"察觉不到，所以上面的报错信息里会把
    当前集合打出来供人工比对；真要钉名单，等这个数降到两位数再说。
    """
    return set()


def test_billing模块已经切完():
    """第一批切的是 `billing.py`——它一条都不许退回去。

    挑 billing 打头是因为它 8 个端点、既有 63 条用例兜底，且业务上"列表少一半"
    的后果直接（对账、结算、押金预警都靠列表看全）。
    """
    left = {e for e in silently_truncating_endpoints() if e.startswith("billing.py:")}
    assert left == set(), f"billing.py 又有端点退回硬编码 .limit()：{sorted(left)}"


#: 第二批人工核过的三处**误报**：`.limit(N)` 在嵌套子查询上，是业务上限不是分页缺陷。
#: 这不是豁免名单（它们照样计入基线），是给下一个人的"别去迁这三条"的记号。
NESTED_CAP_FALSE_POSITIVES = {
    "portal.py:portal_my_contract",
    "spd/portal.py:archive",
    "spd/portal.py:journey",
}


def test_portal两个模块只剩三处误报():
    """第二批切的是 `portal.py` + `spd/portal.py`——23 个已切，剩下的恰好是那三处误报。

    钉成"恰好等于"而不是"包含于"：多出来一条说明有端点退回了硬编码，
    少一条说明有人把嵌套上限当分页缺陷迁掉了——两种都该当场红。
    """
    left = {
        e for e in silently_truncating_endpoints()
        if e.startswith(("portal.py:", "spd/portal.py:"))
    }
    assert left == NESTED_CAP_FALSE_POSITIVES, (
        f"多出来的（退回硬编码）：{sorted(left - NESTED_CAP_FALSE_POSITIVES)}；"
        f"少掉的（嵌套上限被误迁）：{sorted(NESTED_CAP_FALSE_POSITIVES - left)}"
    )
