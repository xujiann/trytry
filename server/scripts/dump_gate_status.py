"""生成 `docs/闸门现状.md`：把各条棘轮的**登记基线**汇到一页。

## 为什么要有这份文件

2026-09-11 这一天里，**三处白纸黑字的声明是假的**：

* `spd/tasks.py:batch_tasks` 的注释说「同文件的单条接口一直是校验的（见 `claim_task`）」
  ——而 `claim_task` 恰恰是没校验的那个；
* `app/clock.py` 的 docstring 说「`test_clock.py` 有一条扫描用例」——那个文件当时并不存在；
* 上一轮的欠账清单注释说 `adjust_path_instance`「修不了，`SpdPathInstance` 没有机构列」
  ——没有机构列是真的，「所以校验不了」是错的。

同一天还发现 `ROADMAP.md` 把**已经清零**的契约棘轮仍写成「portal 余 48」，
于是按路线找活会找到一件已经做完的事。

共同点很清楚：**一句写下来就不再有人回头核的数字**。本仓库对这件事已有成方——
`docs/schema/SCHEMA.md` 配 `test_schema_snapshot_freshness.py`，
把「靠人记得重跑脚本」换成「不一致就红」。这份文件照抄那个形状。

## 它保证什么、不保证什么

**保证**：这页上的数字与各闸门源码里的常量逐字节一致。漂了就红。

**不保证**：常量本身与代码现状一致——那是**各闸门自己那条用例**的职责
（每条都断言「实测 == 基线」）。这里刻意**不**重跑扫描：
再写一遍测量逻辑就是第二份实现，两份实现迟早会飘，
那时这页会在真闸门已经坏掉的情况下继续报绿。
"""
import pathlib
import sys

SERVER = pathlib.Path(__file__).resolve().parents[1]
OUT = SERVER.parent / "docs" / "闸门现状.md"

sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))


def _rows() -> list[tuple[str, str, str, str]]:
    """(分组, 指标, 值, 判据出处)。**只读常量，不重跑扫描**（理由见模块 docstring）。"""
    import test_api_contract_governance as contract
    import test_clock as clock
    import test_list_pagination_ratchet as pagination
    import test_stage14_concurrency as concurrency
    import test_stage15_horizontal as horizontal
    import test_unscopable_patient_reads as unscopable

    H = "tests/test_stage15_horizontal.py"
    return [
        ("横向越权（写侧）", "按 id 写接口机构归属欠账", len(horizontal.NEWLY_VISIBLE_UNGUARDED_WRITES), H),
        ("横向越权（写侧）", "归属隔一跳的无守卫写端点", len(horizontal.ONEHOP_UNGUARDED_WRITES), H),
        ("横向越权（写侧）", "按设计跨机构的豁免（逐条写明理由）", len(horizontal.BYID_CROSS_ORG_OK), H),
        ("横向越权（写侧）", "已登记的领域守卫", len(horizontal.DOMAIN_ORG_GUARDS), H),
        ("横向越权（读侧）", "按 id 读患者资源的豁免", len(horizontal.BYID_PATIENT_READ_OK), H),
        ("横向越权（读侧）", "跟进 helper 后新看见的读侧欠账", len(horizontal.NEWLY_VISIBLE_UNGUARDED_READS), H),
        ("横向越权（读侧）", "无调用方身份的患者读接口（待裁定）",
         len(unscopable.UNSCOPABLE_PATIENT_READS), "tests/test_unscopable_patient_reads.py"),
        ("横向越权（读侧）", "仅聚合无身份（信息项，非欠账）",
         len(unscopable.AGGREGATE_ONLY_READS), "tests/test_unscopable_patient_reads.py"),
        ("接口契约", "缺 response_model 的端点", contract.BASELINE_WITHOUT_RESPONSE_MODEL,
         "tests/test_api_contract_governance.py"),
        ("接口契约", "已完全治理的模块", len(contract.FULLY_GOVERNED),
         "tests/test_api_contract_governance.py"),
        ("列表分页", "仍会静默截断的 GET 端点", pagination.BASELINE_SILENT_TRUNCATION,
         "tests/test_list_pagination_ratchet.py"),
        ("列表分页", "已切 paginate 的端点（反方向棘轮）", len(pagination.PAGINATED_ENDPOINTS),
         "tests/test_list_pagination_ratchet.py"),
        ("列表分页", "等机构/患者收口裁定后才能切", len(pagination.HELD_PENDING_SCOPE_DECISION),
         "tests/test_list_pagination_ratchet.py"),
        ("并发冲突", "写唯一约束表却未处理冲突", len(concurrency.KNOWN_UNGUARDED_UNIQUE_WRITES),
         "tests/test_stage14_concurrency.py"),
        ("并发冲突", "已审计的写入点", concurrency.BASELINE_COVERED_WRITE_SITES,
         "tests/test_stage14_concurrency.py"),
        ("并发冲突", "未审计的写入点", concurrency.BASELINE_UNAUDITED_WRITE_SITES,
         "tests/test_stage14_concurrency.py"),
        ("并发冲突", "待裁定的写入点", concurrency.BASELINE_UNDECIDED_WRITE_SITES,
         "tests/test_stage14_concurrency.py"),
        ("并发冲突", "读-改-写欠账", len(concurrency.KNOWN_READ_MODIFY_WRITE),
         "tests/test_stage14_concurrency.py"),
        ("时间口径", "app/ 里绕过 clock.today() 的 date.today()", clock.DATE_TODAY_BASELINE,
         "tests/test_clock.py"),
        ("时间口径", "app/ 顶层时间快照的豁免", len(clock.APP_IMPORT_TIME_OK),
         "tests/test_clock.py"),
    ]


def render() -> str:
    lines = [
        "# 闸门现状（自动生成，勿手改）",
        "",
        "> 由 `server/scripts/dump_gate_status.py` 生成，",
        "> 由 `server/tests/test_gate_status_freshness.py` 钉住——改了闸门基线却没重跑本脚本，CI 会红。",
        ">",
        "> **数字是各闸门源码里的登记基线。**「基线是否仍等于实测」由各闸门自己那条用例断言；",
        "> 这里不重跑扫描——再写一遍测量逻辑就是第二份实现，两份实现迟早会飘。",
        "",
        "**零基线**（值为 0 的那几行）比「只减不增」强一档：新出现一条就红，不需要有人记得去减。",
        "",
        "| 分组 | 指标 | 当前登记值 | 判据出处 |",
        "|---|---|---:|---|",
    ]
    for group, metric, value, where in _rows():
        lines.append(f"| {group} | {metric} | {value} | `{where}` |")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(), encoding="utf-8")
    print(f"written: {OUT}")


if __name__ == "__main__":
    main()
