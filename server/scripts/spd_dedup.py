"""慢专病 18 张唯一键表的重复处置：先报告，确认后再归并并补建唯一索引。

迁移 `e1a2b3c4d5e9` 只做两件事：干净的表建唯一索引，有重复的表**跳过并落台账**
（`spd_dedup_reports`，`strategy='pending'`）。归并是改账面的动作，按平台通则
（CLAUDE.md §4）不由迁移替人做——这个脚本就是那个"人按下的按钮"。

    python scripts/spd_dedup.py            # 只报告，不改任何数据（默认）
    python scripts/spd_dedup.py --apply    # 归并 + 补建迁移跳过的唯一索引
    python scripts/spd_dedup.py --apply --table spd_point_accounts   # 只处置一张表

## 归并怎么做

- **积分账户**（`spd_point_accounts.user_id`）：`balance`/`earned`/`used` 相加
  并成一条，积分流水、兑换、签到改指到保留行。两个账户都是这个人的，
  相加是唯一不丢积分的并法。
- **其余 17 张**（编码类 + 村医档案）：无法自动判断"两份配置差在哪"，
  保留 **id 最小的那条**（最早建的），其余行整行 JSON 存进台账后移除，
  指向它们的外键改指到保留行。

两种都在台账里留下 `removed_row`（完整原始行），事后能核对被并掉的那份差在哪。
处置完的台账行 `strategy` 从 `pending` 改成 `merge` / `keep_earliest`。

## 退出码

0 = 没有冲突或已处置完；1 = 有冲突待处置（报告模式下），可用于运维巡检。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import sqlalchemy as sa

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))


def _migration():
    """从迁移文件里取 TARGETS / REFERENCES / MERGE_SUM——口径只有一份。

    迁移目录不是 package，按路径加载。名字写死是有意的：这份处置逻辑就是为
    那一条迁移配套的，换了迁移应当换脚本，而不是让脚本去猜。
    """
    path = SERVER / "alembic" / "versions" / "e1a2b3c4d5e9_spd十八张表补唯一索引与去重.py"
    spec = importlib.util.spec_from_file_location("spd_unique_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pending(conn, mig, only: str = "") -> list[tuple[str, str, str, list]]:
    """现场探一遍重复（不读台账——台账可能过期，库里的现状才作数）。"""
    found = []
    for table, index_name, key in mig.TARGETS:
        if only and table != only:
            continue
        groups = mig.duplicate_groups(conn, table, key)
        if groups:
            found.append((table, index_name, key, groups))
    return found


def _index_is_unique(conn, table: str, index_name: str) -> bool:
    info = {i["name"]: i for i in sa.inspect(conn).get_indexes(table)}.get(index_name)
    return bool(info and info.get("unique"))


def report(conn, mig, only: str = "") -> int:
    conflicts = _pending(conn, mig, only)
    if not conflicts:
        print("没有重复：18 张表的唯一键都是干净的。")
        missing = [
            (t, i) for t, i, _k in mig.TARGETS
            if (not only or t == only) and not _index_is_unique(conn, t, i)
        ]
        if missing:
            print(f"但有 {len(missing)} 张表的唯一索引还没建上（迁移当时跳过了）：")
            for table, index_name in missing:
                print(f"  {table}.{index_name}")
            print("跑 --apply 补建。")
            return 1
        return 0

    total = sum(len(ids) - 1 for _t, _i, _k, gs in conflicts for _v, ids in gs)
    print(f"发现 {len(conflicts)} 张表共 {total} 行重复，处置预案如下（当前**未改动任何数据**）：\n")
    for table, index_name, key, groups in conflicts:
        how = "余额与累计相加归并" if table in mig.MERGE_SUM else "保留最早一条，其余存档后移除"
        print(f"■ {table}.{key} —— {how}")
        for value, ids in groups:
            print(f"    {key}={value}：保留 id {ids[0]}，并掉 {ids[1:]}")
        for ref_table, ref_col in mig.REFERENCES.get(table, []):
            print(f"    引用改指：{ref_table}.{ref_col}")
        print(f"    处置后补建唯一索引 {index_name}")
    print("\n确认无误后跑：python scripts/spd_dedup.py --apply")
    return 1


def apply(conn, mig, only: str = "") -> int:
    conflicts = _pending(conn, mig, only)
    merged = 0
    for table, _index_name, key, groups in conflicts:
        refs = mig.REFERENCES.get(table, [])
        strategy = "merge" if table in mig.MERGE_SUM else "keep_earliest"
        for value, ids in groups:
            keep, losers = ids[0], ids[1:]
            for loser in losers:
                snapshot = mig.row_json(conn, table, loser)
                for ref_table, ref_col in refs:      # ① 引用先改指，别删出孤儿
                    conn.execute(
                        sa.text(
                            f"UPDATE {ref_table} SET {ref_col} = :keep "
                            f"WHERE {ref_col} = :loser"
                        ),
                        {"keep": keep, "loser": loser},
                    )
                if table in mig.MERGE_SUM:           # ② 数字并进保留行
                    sums = ", ".join(
                        f"{c} = {c} + (SELECT {c} FROM {table} WHERE id = :loser)"
                        for c in mig.MERGE_SUM[table]
                    )
                    conn.execute(
                        sa.text(f"UPDATE {table} SET {sums} WHERE id = :keep"),
                        {"keep": keep, "loser": loser},
                    )
                conn.execute(                        # ③ 台账落定（没有 pending 行就补一条）
                    sa.text(
                        "UPDATE spd_dedup_reports SET strategy = :s, note = :note "
                        "WHERE table_name = :t AND removed_id = :loser AND strategy = 'pending'"
                    ),
                    {"s": strategy, "loser": loser, "t": table,
                     "note": "已处置" if strategy == "merge" else "已处置，差异见 removed_row"},
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO spd_dedup_reports "
                        "(table_name, key_column, key_value, kept_id, removed_id, "
                        " strategy, removed_row, note, created_at) "
                        "SELECT :t, :kc, :kv, :keep, :loser, :s, :row, '补记：迁移时尚无此冲突', "
                        "       CURRENT_TIMESTAMP "
                        "WHERE NOT EXISTS (SELECT 1 FROM spd_dedup_reports "
                        "                  WHERE table_name = :t AND removed_id = :loser)"
                    ),
                    {"t": table, "kc": key, "kv": str(value), "keep": keep,
                     "loser": loser, "s": strategy, "row": snapshot},
                )
                conn.execute(sa.text(f"DELETE FROM {table} WHERE id = :i"), {"i": loser})
                merged += 1
                print(f"  {table}.{key}={value}：id {loser} 已并入 {keep}")

    built = []
    for table, index_name, key in mig.TARGETS:
        if only and table != only:
            continue
        if _index_is_unique(conn, table, index_name):
            continue
        if mig.duplicate_groups(conn, table, key):   # 还有重复就不建，别把 --table 的漏网当成功
            print(f"  {table} 仍有重复，跳过补建 {index_name}")
            continue
        conn.execute(sa.text(f"DROP INDEX {index_name}"))
        conn.execute(sa.text(f"CREATE UNIQUE INDEX {index_name} ON {table} ({key})"))
        built.append(index_name)

    print(f"\n归并 {merged} 行，补建唯一索引 {len(built)} 个：{'、'.join(built) or '无'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="慢专病唯一键重复处置")
    parser.add_argument("--apply", action="store_true",
                        help="真的归并并补建索引（默认只报告，不改数据）")
    parser.add_argument("--table", default="", help="只处置这一张表")
    parser.add_argument("--json", action="store_true", help="报告用 JSON 输出（巡检用）")
    args = parser.parse_args()

    from app.database import engine

    mig = _migration()
    with engine.begin() as conn:
        if args.json:
            conflicts = _pending(conn, mig, args.table)
            print(json.dumps(
                [{"table": t, "key": k,
                  "groups": [{"value": str(v), "keep": ids[0], "losers": ids[1:]}
                             for v, ids in gs]}
                 for t, _i, k, gs in conflicts],
                ensure_ascii=False, indent=2,
            ))
            return 1 if conflicts else 0
        return apply(conn, mig, args.table) if args.apply else report(conn, mig, args.table)


if __name__ == "__main__":
    raise SystemExit(main())
