"""生成 `docs/模块完成度.md`：87 个路由模块各自离「功能完整」还差什么，一页看全。

## 为什么要有这份文件

`docs/功能完善开发规则.md` 给"一个模块什么时候算做完"下了七项定义，其中五项是机检的，
但判据分散在五个闸门文件里，各自只报一个总数——没有一处能回答"**这个模块**还差什么"。
于是"完善各模块功能"没有工作清单，只能凭印象挑。这份表按模块把五项机检结果并排，
再附上无入口路径的明细：挑批次从这里挑，做完一个模块这一行就该归零。

## 数据从哪来（与 `dump_gate_status.py` 同一纪律：不写第二份测量逻辑）

* **入口**：调用 `tests/test_orphan_endpoints.py` 里的同一个函数现算——闸门与本表用的
  是同一段代码扫同一棵树，不会互相飘；分母是契约闸门的 `_iter_endpoints()`。
* **契约 / 分页 / 越权豁免 / 待裁读接口 / 读改写**：只读各闸门源码里的登记名单
  （`FULLY_GOVERNED` / `PAGINATED_ENDPOINTS` / `BYID_*_OK` / `UNSCOPABLE_PATIENT_READS` /
  `KNOWN_READ_MODIFY_WRITE`），按 `file.py:func` 键的前缀归到模块。名单是否仍等于实测，
  由各闸门自己那条用例断言，这里不重跑。

渲染是确定性的（不带时间戳、集合全部排序），改了代码或名单就重跑：

    cd server && python scripts/dump_module_completeness.py

`tests/test_module_completeness_freshness.py` 钉住"文档 == 重新渲染"，忘了重跑 CI 会红。
"""
from __future__ import annotations

import collections
import pathlib
import sys

SERVER = pathlib.Path(__file__).resolve().parents[1]
OUT = SERVER.parent / "docs" / "模块完成度.md"

sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))


def _belongs(key: str, module: str) -> bool:
    """`billing.py:list_x` 归 `billing`；`spd/config/teams.py:x` 归 `spd/config`。"""
    return key.startswith(module + ".py:") or key.startswith(module + "/")


def _count(keys, module: str) -> int:
    return sum(1 for k in keys if _belongs(k, module))


def rows() -> list[dict]:
    import test_api_contract_governance as contract
    import test_list_pagination_ratchet as pagination
    import test_orphan_endpoint_verbs as orphanverbs
    import test_orphan_endpoints as orphan
    import test_stage14_concurrency as concurrency
    import test_stage15_horizontal as horizontal
    import test_unscopable_patient_reads as unscopable

    endpoints: dict[str, int] = collections.Counter()
    tags: dict[str, set[str]] = collections.defaultdict(set)
    for module, route in contract._iter_endpoints():
        if route.path.startswith("/api/"):
            endpoints[module] += 1
            tags[module].update(route.tags or [])
    paths = orphan.endpoint_paths()
    by_module_paths: dict[str, set[str]] = collections.defaultdict(set)
    for path, module in paths.items():
        by_module_paths[module].add(path)
    debt = orphan.unexplained_orphans()
    exempt_paths = set(orphan.EXEMPT_PATHS)

    out = []
    for module in sorted(endpoints):
        mine = by_module_paths[module]
        out.append({
            "module": module,
            "tags": " / ".join(sorted(tags[module])),
            "endpoints": endpoints[module],
            "paths": len(mine),
            "orphans": sorted(p for p in mine if p in debt),
            "verb_gaps": sorted(v for v in orphanverbs.KNOWN if v.split(" ", 1)[1] in mine),
            "exempt_module": module in orphan.EXEMPT_MODULES,
            "exempt_paths": sorted(p for p in mine if p in exempt_paths),
            "contract": module in contract.FULLY_GOVERNED,
            "paginated": _count(pagination.PAGINATED_ENDPOINTS, module),
            "held": _count(pagination.HELD_PENDING_SCOPE_DECISION, module),
            "authz_exempt": _count(horizontal.BYID_CROSS_ORG_OK, module)
            + _count(horizontal.BYID_PATIENT_READ_OK, module),
            "authz_read_debt": _count(horizontal.NEWLY_VISIBLE_UNGUARDED_READS, module),
            "unscopable": _count(unscopable.UNSCOPABLE_PATIENT_READS, module),
            "rmw": _count(concurrency.KNOWN_READ_MODIFY_WRITE, module),
        })
    return out


def render() -> str:
    data = rows()
    total_paths = sum(r["paths"] for r in data)
    total_debt = sum(len(r["orphans"]) for r in data)
    total_verb_gaps = sum(len(r["verb_gaps"]) for r in data)
    total_exempt = sum(len(r["exempt_paths"]) for r in data) + sum(
        r["paths"] for r in data if r["exempt_module"]
    )
    judged = [r for r in data if not r["exempt_module"]]
    clean = [r for r in judged if not r["orphans"]]
    lines = [
        "# 模块完成度（自动生成，勿手改）",
        "",
        "> 由 `server/scripts/dump_module_completeness.py` 生成，",
        "> 由 `server/tests/test_module_completeness_freshness.py` 钉住——改了代码或闸门名单却没重跑本脚本，CI 会红。",
        ">",
        "> 一行一个路由模块。**「无入口」是现算的**（与 `tests/test_orphan_endpoints.py` 同一个函数），",
        "> 其余欠账列**只读各闸门源码里的登记名单**（名单是否仍等于实测由各闸门自己断言，这里不重跑）。",
        "> 七项完成定义见 `docs/功能完善开发规则.md` §1；这张表覆盖其中机检的五项，",
        "> 「有测试」「有演示数据与手册条目」两项仍靠评审。",
        "",
        "## 总览",
        "",
        "| 口径 | 数 |",
        "|---|---:|",
        f"| 路由模块 | {len(data)} |",
        f"| `/api` 路径（不同路径数） | {total_paths} |",
        f"| 无入口路径（欠账，`KNOWN_ORPHANS`） | {total_debt} |",
        f"| 路径有入口、写动词没有（欠账，`test_orphan_endpoint_verbs.KNOWN`） | {total_verb_gaps} |",
        f"| 按设计无界面（`EXEMPT_*`，含整模块豁免） | {total_exempt} |",
        f"| 无入口为 0 的模块 | {len(clean)} / {len(judged)}（不含整模块豁免） |",
        "",
        "## 逐模块",
        "",
        "按「无入口」降序、再按「动词缺口」降序——挑批次从上往下挑；同数按模块名。",
        "「动词缺口」是路径有入口、某个写动词（多是「新建」）没有入口的端点数（登记名单，见下方明细）。",
        "「越权豁免」是逐条写了理由的按设计跨机构/按 id 读的端点数（不是欠账），",
        "「读侧欠账」「待裁读接口」「读改写」是尚未关掉的账。",
        "",
        "| 模块 | 标签 | 端点 | 路径 | 无入口 | 动词缺口 | 契约 | 分页 已切/待裁 | 越权豁免 | 读侧欠账 | 待裁读接口 | 读改写 |",
        "|---|---|---:|---:|---:|---:|:-:|---:|---:|---:|---:|---:|",
    ]
    ordered = sorted(data, key=lambda r: (-len(r["orphans"]), -len(r["verb_gaps"]), r["module"]))
    for r in ordered:
        orphans = "豁免" if r["exempt_module"] else str(len(r["orphans"]))
        contract = "✅" if r["contract"] else "✗"
        lines.append(
            f"| `{r['module']}` | {r['tags']} | {r['endpoints']} | {r['paths']} | {orphans} | {len(r['verb_gaps'])} | {contract} "
            f"| {r['paginated']} / {r['held']} | {r['authz_exempt']} | {r['authz_read_debt']} "
            f"| {r['unscopable']} | {r['rmw']} |"
        )
    lines += [
        "",
        "## 无入口路径明细（按模块）",
        "",
        "接通一条就把它从 `tests/test_orphan_endpoints.py::KNOWN_ORPHANS` 里划掉，再重跑本脚本。",
        "按设计不需要界面的**不要**留在这里——进 `EXEMPT_PATHS` 并写明理由。",
        "",
    ]
    for r in ordered:
        if not r["orphans"]:
            continue
        lines.append(f"- **`{r['module']}`**（{len(r['orphans'])}）")
        lines.extend(f"  - `{p}`" for p in r["orphans"])
    lines += [
        "",
        "## 写动词无入口明细（按模块）",
        "",
        "路径有入口（清单或详情有页面在调），这个写动词没有——多是配置项只能改不能建。接上一条就把它从",
        "`tests/test_orphan_endpoint_verbs.py::KNOWN` 里划掉，再重跑本脚本。",
        "",
    ]
    for r in ordered:
        if r["verb_gaps"]:
            lines.append(f"- **`{r['module']}`**（{len(r['verb_gaps'])}）")
            lines.extend(f"  - `{v}`" for v in r["verb_gaps"])
    exempt_lines = []
    for r in sorted(data, key=lambda r: r["module"]):
        if r["exempt_module"]:
            exempt_lines.append(f"- **`{r['module']}`**（整模块，{r['paths']} 条）")
        for p in r["exempt_paths"]:
            exempt_lines.append(f"- `{p}`（`{r['module']}`）")
    lines += ["", "## 按设计无界面（书面豁免，理由见闸门源码）", ""] + exempt_lines + [""]
    return "\n".join(lines)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(), encoding="utf-8")
    print(f"written: {OUT}")


if __name__ == "__main__":
    main()
