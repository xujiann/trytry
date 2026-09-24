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
    import test_ascii_digit_patterns as asciidigits
    import test_body_fk_exists as bodyfk
    import test_body_finite_numbers as bodyfinite
    import test_body_numeric_capacity as bodynumcap
    import test_body_raw_dict as bodyraw
    import test_date_window_bounds as datewin
    import test_query_limit_params as limitparams
    import test_response_constraint_writers as respwriters
    import test_body_str_length as bodystr
    import test_body_declared_org_write_guard as declared
    import test_org_param_read_guard as orgread
    import test_byid_org_read_guard as byidread
    import test_secondary_body_id_guard as secondary
    import test_body_id_org_write_guard as bodyid
    import test_clock as clock
    import test_date_query_params as datequery
    import test_frontend_prompt_guard as promptguard
    import test_datestr_single_source as datestr
    import test_frontend_destructive_confirm_guard as destructive
    import test_frontend_api_calls_resolve as apicalls
    import test_filter_ids_not_truncated as truncids
    import test_list_pagination_ratchet as pagination
    import test_orphan_endpoints as orphan
    import test_periodstr_single_source as periodstr
    import test_query_param_validators as queryvalidators
    import test_stage14_concurrency as concurrency
    import test_stage15_horizontal as horizontal
    import test_unscopable_patient_reads as unscopable

    H = "tests/test_stage15_horizontal.py"
    return [
        ("横向越权（写侧）", "按 id 写接口机构归属欠账", len(horizontal.NEWLY_VISIBLE_UNGUARDED_WRITES), H),
        ("横向越权（写侧）", "归属隔一跳的无守卫写端点", len(horizontal.ONEHOP_UNGUARDED_WRITES), H),
        ("横向越权（写侧）", "挂在患者上的表（含隔一跳）× 按 id 写无守卫（待裁定）",
         len(horizontal.PATIENT_OWNED_UNGUARDED_WRITES), H),
        ("横向越权（写侧）", "挂在患者上的表（含隔一跳）× 按设计不阻断（逐条写明理由）",
         len(horizontal.PATIENT_OWNED_BY_DESIGN), H),
        ("横向越权（写侧）", "新建挂在患者上、无机构列的行 × 按设计不判（逐条写明理由）",
         len(horizontal.PATIENT_OWNED_CREATE_BY_DESIGN), H),
        ("横向越权（写侧）", "新建挂在患者上、无机构列的行 × 口径待裁定",
         len(horizontal.PATIENT_OWNED_CREATE_AWAITING), H),
        ("横向越权（写侧）", "按 id 写别名机构列（center_org_id 等）的表 × 按设计不判（逐条写明理由）",
         len(horizontal.ALIAS_ORG_BYID_BY_DESIGN), H),
        ("横向越权（写侧）", "按设计跨机构的豁免（逐条写明理由）", len(horizontal.BYID_CROSS_ORG_OK), H),
        ("横向越权（写侧）", "按 id 取的主对象没有机构列、命中的只是外键存在性检查（跨机构配置）",
         len(horizontal.BYID_PRIMARY_WITHOUT_ORG), H),
        ("横向越权（写侧）", "已登记的领域守卫", len(horizontal.DOMAIN_ORG_GUARDS), H),
        ("横向越权（读侧）", "按 id 读患者资源的豁免", len(horizontal.BYID_PATIENT_READ_OK), H),
        ("横向越权（读侧）", "跟进 helper 后新看见的读侧欠账", len(horizontal.NEWLY_VISIBLE_UNGUARDED_READS), H),
        ("横向越权（读侧）", "无调用方身份的患者读接口（待裁定）",
         len(unscopable.UNSCOPABLE_PATIENT_READS), "tests/test_unscopable_patient_reads.py"),
        ("横向越权（读侧）", "仅聚合无身份（信息项，非欠账）",
         len(unscopable.AGGREGATE_ONLY_READS), "tests/test_unscopable_patient_reads.py"),
        ("横向越权（读侧）", "无调用方身份 × 隔一跳/两跳的患者维度表（待逐条判定）",
         len(unscopable.ONEHOP_UNSCOPABLE_READS), "tests/test_unscopable_patient_reads.py"),
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
        ("列表分页", "筛选用的 id 名单先截断再用（搜索名单静默少一截）", truncids.BASELINE,
         "tests/test_filter_ids_not_truncated.py"),
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
        ("引用完整性", "请求体外键原样写库、函数里一眼不看（撞外键被翻成 409 误报 / 500）", bodyfk.BASELINE,
         "tests/test_body_fk_exists.py"),
        ("引用完整性", "请求体字符串无长度上限写进定长列（PG 上超长即 500；197 → 0，第二层循环写库 +4、第三层显式赋值 +10、第四层查出对象 / 字面量构造 / 原样取值 +16、第五层转一手再写 +37 → 0）", bodystr.BASELINE,
         "tests/test_body_str_length.py"),
        ("数值入参", "浮点入参收得下 NaN / Infinity（超标判定对 NaN 恒为假、金额列 500；66 → 0 已清零）",
         bodyfinite.BASELINE, "tests/test_body_finite_numbers.py"),
        ("数值入参", "整数 / 金额入参越过列容量写进 Integer / Money 列（PG 上溢出即 500；65 → 0，第三层显式赋值 +1、第四层 +3、可空 Annotated 盲区 +1、第五层转一手再写 +23 → 0）",
         bodynumcap.BASELINE, "tests/test_body_numeric_capacity.py"),
        ("数值入参", "天数 / 分钟数入参没有上界就进 timedelta（传个大数即日期溢出、500；5 → 0 已清零）",
         datewin.BASELINE, "tests/test_date_window_bounds.py"),
        ("数值入参", "条数 / 偏移量入参没有上下界就进 .limit() / .offset()（PG 上负数即 500；2 → 0 已清零）",
         limitparams.BASELINE, "tests/test_query_limit_params.py"),
        ("请求体契约", "写接口收裸 dict 请求体（改档绕过建档的一切校验，坏值落库后整张列表 500；20 → 0 已清零）",
         bodyraw.BASELINE, "tests/test_body_raw_dict.py"),
        ("请求体契约", "按设计收原样资源的入站端点（FHIR R4 资源，逐条写明理由）",
         len(bodyraw.BY_DESIGN), "tests/test_body_raw_dict.py"),
        ("请求体契约", "可空入参写进不可空列、处理函数也不挡 None（显式传 null 即 500；30 → 0 已清零）",
         bodyraw.NULLABLE_BASELINE, "tests/test_body_raw_dict.py"),
        ("请求体契约", "写同一列的入口比出参约束松（存得进去、读不出来，库里一行就让整个响应 500；1 → 0 已清零）",
         respwriters.BASELINE, "tests/test_response_constraint_writers.py"),
        ("时间口径", "app/ 里绕过 clock.today() 的 date.today()", clock.DATE_TODAY_BASELINE,
         "tests/test_clock.py"),
        ("时间口径", "app/ 顶层时间快照的豁免", len(clock.APP_IMPORT_TIME_OK),
         "tests/test_clock.py"),
        ("横向越权（写侧）", "body 收 id 的无守卫写端点（候选，未逐条判定）",
         len(bodyid.KNOWN_BODY_ID_WRITES), "tests/test_body_id_org_write_guard.py"),
        ("横向越权（写侧）", "按设计无调用方身份的豁免（body 收 id 那一族）",
         len(bodyid.EXEMPT), "tests/test_body_id_org_write_guard.py"),
        ("横向越权（写侧）", "角色门只允许全域角色（非可越权入口）",
         len(bodyid.GLOBAL_ROLE_ONLY), "tests/test_body_id_org_write_guard.py"),
        ("横向越权（写侧）", "被写对象没有单一机构归属（跨机构配置，表上无 org_id）",
         len(bodyid.NO_SINGLE_ORG_OWNER), "tests/test_body_id_org_write_guard.py"),
        ("横向越权（写侧）", "请求声明机构、却无归属判定的写端点（候选，只减不增）",
         len(declared.KNOWN_UNGUARDED), "tests/test_body_declared_org_write_guard.py"),
        ("横向越权（写侧）", "请求里的机构按设计就是别家（逐条写明理由）",
         len(declared.BY_DESIGN), "tests/test_body_declared_org_write_guard.py"),
        ("横向越权（写侧）", "请求声明机构 × 早已登记待业务裁定、故意未修",
         len(declared.AWAITING_DECISION), "tests/test_body_declared_org_write_guard.py"),
        ("横向越权（写侧）", "请求声明的机构字段按设计是对方（接收方 / 受邀方；字段级，P0-43）",
         len(declared.COUNTERPART_FIELDS), "tests/test_body_declared_org_write_guard.py"),
        ("横向越权（写侧）", "声明机构字段级 × 早已登记待裁定（P1-56 派驻的派出机构）",
         len(declared.FIELD_AWAITING_DECISION), "tests/test_body_declared_org_write_guard.py"),
        ("横向越权（写侧）", "请求体捎带的机构归属对象未判 × 按设计（逐条写明理由）",
         len(secondary.BY_DESIGN), "tests/test_secondary_body_id_guard.py"),
        ("横向越权（写侧）", "请求体捎带的机构归属对象未判 × 口径待裁定",
         len(secondary.AWAITING), "tests/test_secondary_body_id_guard.py"),
        ("横向越权（读侧）", "查询参数收机构号、只拿 resolve_org_scope 当范围 × 按设计（逐条写明理由）",
         len(orgread.BY_DESIGN), "tests/test_org_param_read_guard.py"),
        ("横向越权（读侧）", "查询参数收机构号、只拿 resolve_org_scope 当范围 × 口径待裁定",
         len(orgread.AWAITING), "tests/test_org_param_read_guard.py"),
        ("横向越权（读侧）", "按 id 读机构归属表、无读侧判定 × 按设计（逐条写明理由）",
         len(byidread.BY_DESIGN), "tests/test_byid_org_read_guard.py"),
        ("横向越权（读侧）", "按 id 读机构归属表、无读侧判定 × 患者数据已在别的欠账名单",
         len(byidread.ELSEWHERE), "tests/test_byid_org_read_guard.py"),
        ("横向越权（读侧）", "按 id 读机构归属表、无读侧判定 × 口径待裁定",
         len(byidread.AWAITING), "tests/test_byid_org_read_guard.py"),
        ("横向越权（读侧）", "按路径参数读、不经机构 / 患者表、无读侧判定（盲区）× 按设计",
         len(byidread.BLINDSPOT_BY_DESIGN), "tests/test_byid_org_read_guard.py"),
        ("横向越权（读侧）", "按路径参数读、不经机构 / 患者表、无读侧判定（盲区）× 患者数据已在别的欠账名单",
         len(byidread.BLINDSPOT_ELSEWHERE), "tests/test_byid_org_read_guard.py"),
        ("横向越权（读侧）", "按路径参数读、不经机构 / 患者表、无读侧判定（盲区）× 口径待裁定",
         len(byidread.BLINDSPOT_AWAITING), "tests/test_byid_org_read_guard.py"),
        ("日期入参", "未经 require_date / resolve_business_date 的日期查询参数",
         len(datequery.KNOWN_BARE_DATE_PARAMS), "tests/test_date_query_params.py"),
        ("日期入参", "请求体里注解为裸 str 的日期字段", len(datestr.KNOWN_BARE_BODY_DATE_FIELDS),
         "tests/test_datestr_single_source.py"),
        ("日期入参", "未经校验的月度期间查询参数", len(periodstr.KNOWN_BARE_MONTH_PARAMS),
         "tests/test_periodstr_single_source.py"),
        ("日期入参", "名字像期间、按设计不是 YYYY-MM 的豁免", len(periodstr.NOT_A_MONTH),
         "tests/test_periodstr_single_source.py"),
        ("日期入参", "带校验器的类型别名写成 `= Query(...)`（FastAPI 丢掉校验器）", queryvalidators.BASELINE,
         "tests/test_query_param_validators.py"),
        ("日期入参", "`pattern=` 里用 `\\d`（认全角 / 阿拉伯-印度数字）", asciidigits.BASELINE,
         "tests/test_ascii_digit_patterns.py"),
        ("功能完善", "该有界面却没有的端点（孤儿）", len(orphan.KNOWN_ORPHANS),
         "tests/test_orphan_endpoints.py"),
        ("功能完善", "按设计不需要界面的豁免路径", len(orphan.EXEMPT_PATHS),
         "tests/test_orphan_endpoints.py"),
        ("功能完善", "前端写出来的 `/api` 地址没有路由 / 动词后端不接 / 判不了（反方向）", apicalls.BASELINE,
         "tests/test_frontend_api_calls_resolve.py"),
        ("功能完善", "前端含 prompt( 的行（录入不靠弹窗）", promptguard.BASELINE,
         "tests/test_frontend_prompt_guard.py"),
        ("功能完善", "免确认的破坏性操作（可逆，逐条写明怎么恢复）", len(destructive.EXEMPT),
         "tests/test_frontend_destructive_confirm_guard.py"),
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
