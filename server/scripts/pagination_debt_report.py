#!/usr/bin/env python3
"""把 P2-8 的「还剩多少」拆成**四类形状**，让 P1-49 变得可裁定。

`tests/test_list_pagination_ratchet.py` 只给一个总数，而那个数把两件性质完全不同的
事混在一起：一件是**分页整改**（补 `offset/limit` 就完事），一件是**「谁该看见全部」
的授权裁定**（切了就把可枚举面从「最多 N 行」放大成「整表可翻」）。
拿一个混合数去找人拍板，谁也拍不了。

本脚本按**表里有没有归属维度**把剩余端点分成四类：

  A 已有收口          → 分页整改，可直接切
  B 表里有机构列却没收口 → 补一行守卫即可；但「该不该收」仍要人定
  C 归属要经一跳外键     → 结构性盲区（横向越权闸门的分母也漏这一类）
  D 表里没有任何归属维度 → **需人裁**：是全域配置，还是归属从未定义（P1-35 族）

**只有 D 是真正开放的问题**，另外三类各自有明确的下一步。

⚠️ **这个分类自己被改对过一次，值得照抄这条教训。** 第一版判据是
「表里没有 `org_id` 也没有 `patient_id` → 配置目录」，跑出来 62 条"配置目录"，
里面赫然是 `PayrollRecord`（薪酬）、`StaffContract`（劳动合同）、
`SterilizationBatch`（消毒批次）——薪酬当然不是配置目录。原因是归属列**不叫**
`org_id`：`PayrollRecord` 靠 `employee_id` 一跳到人再到机构，`SterilizationBatch`
叫 `center_org_id`。判据比缺陷窄，于是给出一个**看着很确定的错答案**。
现在按「列名里含 org_id」+「到人的外键」两条一起认，并且把认不出归属维度的
显式留成 D 类**交给人**，而不是自动判成"安全"。

用法：`python scripts/pagination_debt_report.py`（只读，不改任何文件）
"""
import ast
import collections
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))

from app.database import Base  # noqa: E402

import app.models  # noqa: E402,F401
import app.spd.models  # noqa: E402,F401
from test_list_pagination_ratchet import (  # noqa: E402
    NESTED_CAP_FALSE_POSITIVES,
    _router_files,
    silently_truncating_endpoints,
)

#: 认得出的收口调用。**这张表漏一个名字，就会把一个有收口的端点误报成没有**
#: —— `assert_patient_visible` 就这样被漏过一次，让统计报成 21/104（实为 26/97）。
GUARDS = (
    "scope_org_list", "scope_patient_list", "visible_org_ids", "stats_org_ids",
    "accessible_patient", "assert_patient_visible", "assert_org_writable",
    "log_patient_access", "_patient(", "current_resident",
)
#: 「一跳到人」的外键。`created_by` 不算——那是操作者不是归属。
PERSON_FK = ("patient_id", "employee_id", "user_id", "account_id", "doctor_id")


def classify() -> dict:
    models = {m.class_.__name__: m.class_ for m in Base.registry.mappers}
    bodies = {}
    for name, path in _router_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            bodies[f"{name}:{fn.name}"] = ast.unparse(fn)

    buckets = collections.defaultdict(list)
    for endpoint in sorted(silently_truncating_endpoints() - NESTED_CAP_FALSE_POSITIVES):
        body = bodies.get(endpoint, "")
        if any(w in body for w in GUARDS):
            buckets["A"].append(endpoint)
            continue
        match = re.search(r"db\.query\((\w+)", body)
        model = models.get(match.group(1)) if match else None
        if model is None:
            buckets["X"].append(endpoint)
            continue
        columns = set(model.__table__.columns.keys())
        org = sorted(c for c in columns if "org_id" in c)
        person = sorted(c for c in columns if c in PERSON_FK)
        if org:
            buckets["B"].append(f"{endpoint}  [{model.__name__}.{org[0]}]")
        elif person:
            buckets["C"].append(f"{endpoint}  [{model.__name__}.{person[0]}]")
        else:
            buckets["D"].append(f"{endpoint}  [{model.__name__}]")
    return buckets


LABELS = {
    "A": "已有收口 → 分页整改，可直接切",
    "B": "表里有机构列却没收口 → 补一行守卫即可（「该不该收」仍要人定）",
    "C": "归属要经一跳外键 → 结构性盲区，与 P1-35/P1-48 同族",
    "D": "表里没有任何归属维度 → **需人裁**：全域配置，还是归属从未定义",
    "X": "认不出主表 → 人工看",
}


def main() -> int:
    buckets = classify()
    total = sum(len(v) for v in buckets.values())
    print(f"P2-8 剩余（已除去 {len(NESTED_CAP_FALSE_POSITIVES)} 处判据误报）：{total} 个端点\n")
    for key in ("A", "B", "C", "D", "X"):
        rows = buckets.get(key, [])
        if not rows:
            continue
        print(f"[{key}] {len(rows):3d} 个 —— {LABELS[key]}")
        for row in rows:
            print(f"      {row}")
        print()
    print("只有 D 类是真正开放的问题；A 可直接做，B/C 各有明确的下一步。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
