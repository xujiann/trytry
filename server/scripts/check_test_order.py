"""查"用例之间靠执行顺序互相喂数据"——逐模块正序跑一遍、倒序再跑一遍（P1-55）。

## 这查的是什么，代价在哪

本仓库大量模块是**按场景写的**：第一条用例建机构、第二条基于它下单、第三条断言
结果。只要从头跑到尾就全绿，所以谁也不会发现——直到有人想**单独跑其中一条**：

    $ pytest tests/test_stage95_batch1.py::test_暂存点位不能当产生点用
    IndexError: list index out of range

报的错和被测的东西毫无关系（它要的暂存点位是前一条用例建的）。而调试一个红掉的
用例时，第一件想做的事恰恰就是把它单独拎出来跑。这是这笔账每天都在收的利息。

第二笔代价是**用例可能为了错的理由而绿**：它断言的那行数据是别人建的，
换个顺序就没有了——那么它究竟测住了什么，没人答得上来。

## 为什么用倒序，而不是随机打乱

1. **倒序确定可复现**，不必记种子、不会今天红明天绿；
2. `pytest-randomly` **不在 `requirements-dev.txt` 里**。工具不该依赖一个本仓库
   并不安装的插件——本机碰巧装了是本机的事，CI 上没有。

倒序不是万能的：A→B→C 里 C 只依赖 A 的情况，倒序 C→B→A 照样红（能查出来），
但"两条互不相干的用例恰好换个位置也没事"这种它也不会说什么。它是**下界**，
不是全集。如实写在这里，别把它当成"顺序无关"的证明。

## 用法

    python scripts/check_test_order.py            # 全量（约 5 分钟，8 个并发）
    python scripts/check_test_order.py -j 4       # 指定并发
    python scripts/check_test_order.py tests/test_rbac.py ...   # 只查几个

退出码非 0 的两种情况：
- **新增了顺序依赖**（不在基线里的模块倒序红了）——这是要拦的；
- **基线里的模块倒序绿了**——说明已经修好了，把它从基线里删掉（棘轮只许收紧）。
"""
import argparse
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
TESTS = SERVER / "tests"

#: 已知**倒序会红**的模块：模块内的用例靠前后顺序互相喂数据。
#: 这是 P1-55 的欠账清单，**只许变少**。修一个删一条。
#:
#: 修法通常不大：把"前一条用例顺手建出来的东西"提成模块级 fixture。
#: 判断哪种是真该修：断言依赖别人**建的数据**（IndexError / 404 / 空列表）是该修的；
#: 少数模块是刻意的端到端剧本（一条主流程拆成若干步），那种要在模块 docstring 里
#: 写明"本模块按剧本顺序跑"，再登记在这里。
KNOWN_ORDER_DEPENDENT = {
    "test_accounting_cost.py",
    "test_analytics.py",
    "test_checkup_items_review.py",
    "test_clinical_indicators.py",
    "test_clinical_surgery.py",
    "test_consents.py",
    "test_cssd_improvement_contracts.py",
    "test_dataquality.py",
    "test_deepen_exams_rx.py",
    "test_deepen_public.py",
    "test_dictionary_import.py",
    "test_doctor_mobile.py",
    "test_drg_expansion.py",
    "test_drug_rules_seed.py",
    "test_esb.py",
    "test_esb_outbound.py",
    "test_fhir_depth.py",
    "test_final_gap3.py",
    "test_guideline.py",
    "test_import_charge_items.py",
    "test_import_legacy.py",
    "test_import_legacy_bulk.py",
    "test_import_users.py",
    "test_inpatient_order_executions.py",
    "test_integration.py",
    "test_integration_hl7_depth.py",
    "test_medical_record_qc.py",
    "test_metrics_drilldown.py",
    "test_modules.py",
    "test_modules2.py",
    "test_notifications.py",
    "test_outpatient_docs.py",
    "test_performance_orgs_contract.py",
    "test_pharmacy_batches.py",
    "test_platform_console.py",
    "test_platform_engines.py",
    "test_portal_me_contract.py",
    "test_portal_services.py",
    "test_printing.py",
    "test_printing_documents.py",
    "test_rbac.py",
    "test_realtime.py",
    "test_scheduler.py",
    "test_security.py",
    "test_service_extras_split_contract.py",
    "test_spd_assess_metrics.py",
    "test_spd_config_admin.py",
    "test_spd_config_catalog_contract.py",
    "test_spd_config_paths_devices_contract.py",
    "test_spd_flow.py",
    "test_spd_integration.py",
    "test_spd_portal.py",
    "test_spd_portal_contract.py",
    "test_staffing_disease.py",
    "test_stage10_resources.py",
    "test_stage11_security.py",
    "test_stage15_horizontal.py",
    "test_stage4_billing.py",
    "test_stage4_exchange.py",
    "test_stage4_inpatient.py",
    "test_stage95_batch1.py",
    "test_stage95_batch2.py",
    "test_stage95_batch3.py",
}


def _run(path: Path, workdir: Path) -> tuple[str, bool, str]:
    """倒序跑一个模块。返回 (模块名, 是否通过, 末行摘要)。"""
    env = dict(os.environ, MEDPLAT_TEST_REVERSE="1")
    # 每个并发单元一个独立 cwd：测试库是 `sqlite:///./test_run.db`，按 cwd 落盘，
    # 共用一个目录会让并发的模块互相踩掉对方的库。
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(path), "-q", "-p", "no:randomly"],
        cwd=workdir, env=env, capture_output=True, text=True, timeout=600,
    )
    tail = (proc.stdout.strip().splitlines() or ["(无输出)"])[-1]
    return path.name, proc.returncode == 0, tail


def main() -> int:
    parser = argparse.ArgumentParser(description="逐模块倒序跑，查用例间的顺序依赖")
    parser.add_argument("paths", nargs="*", help="只查这些模块（默认全部）")
    parser.add_argument("-j", "--jobs", type=int, default=8, help="并发数（默认 8）")
    args = parser.parse_args()

    modules = [Path(p) for p in args.paths] or sorted(TESTS.glob("test_*.py"))
    if not modules:
        print("没有找到任何测试模块——路径写错了？", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        workdirs = []
        for i in range(args.jobs):
            d = Path(tmp) / f"w{i}"
            d.mkdir()
            for name in ("app", "tests", "alembic"):
                (d / name).symlink_to(SERVER / name)
            (d / "pyproject.toml").symlink_to(SERVER / "pyproject.toml")
            workdirs.append(d)

        results = []
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = [
                pool.submit(_run, m, workdirs[i % args.jobs])
                for i, m in enumerate(modules)
            ]
            for done, fut in enumerate(futures, 1):
                results.append(fut.result())
                print(f"\r  已跑 {done}/{len(modules)}", end="", file=sys.stderr)
        print("", file=sys.stderr)

    failing = {name for name, ok, _tail in results if not ok}
    checked = {m.name for m in modules}
    new = sorted(failing - KNOWN_ORDER_DEPENDENT)
    fixed = sorted((KNOWN_ORDER_DEPENDENT & checked) - failing)

    print(f"\n逐模块倒序：{len(modules)} 个模块，{len(failing)} 个倒序变红"
          f"（基线 {len(KNOWN_ORDER_DEPENDENT & checked)}）")
    for name, ok, tail in sorted(results):
        if not ok:
            mark = "新增" if name in new else "已登记"
            print(f"  [{mark}] {name}: {tail}")

    if new:
        print("\n✗ 新增了顺序依赖，这些模块里有用例在靠前面那条先跑过：")
        for name in new:
            print(f"    {name}")
        print("  修法：把前一条用例顺手建出来的东西提成 fixture；"
              "确实是端到端剧本的，在模块 docstring 里写明并登记进基线。")
    if fixed:
        print("\n✗ 这些模块已经不依赖顺序了，请从 KNOWN_ORDER_DEPENDENT 里删掉："
              f"\n    {fixed}\n  （棘轮只许收紧，留着陈旧条目等于把口子敞着）")
    if not new and not fixed:
        print("\n✓ 与基线一致")
    return 1 if (new or fixed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
