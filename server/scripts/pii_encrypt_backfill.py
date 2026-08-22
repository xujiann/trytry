#!/usr/bin/env python3
"""PII 存量加密回填（工程包 E3）：把明文 PII 列改写为 pii1$ 密文并重算检索索引。

作用列：patients.id_card / patients.phone / resident_accounts.phone。

## 运维口径（顺序敏感）

1. 先跑迁移 a4b5c6d7e8f9（加 *_idx 列并回填索引）；
2. 跑本脚本把存量明文改写为密文（此时开关仍关，读取按前缀透明解密，业务不受影响）；
3. 设 MEDPLAT_PII_ENCRYPTION_ENABLED=true，重启应用——新写入落密文。

## 密钥轮换（--old-secret）

加密列一律派生自**当前** MEDPLAT_SECRET。换钥后存量密文用旧钥加密，读取
虽有 secret_previous 回退（见 app/pii.py），但检索索引没有多口径——换钥后
应**立即**重跑本脚本并带 ``--old-secret <旧值>``：旧钥密文解开、当前钥重加密、
索引按当前钥重算；宽限期结束即可清掉 MEDPLAT_SECRET_PREVIOUS。

## 检索索引重建（--rebuild-index）

**只重算 `*_idx`，不碰密文列。**为什么需要单独一个模式：默认（加密）模式对
带 pii1$ 前缀的行是"幂等跳过"——跳过时**连索引也不重算**，于是索引一旦丢了
或算错，这个脚本自己修不回来，EMPI 去重就永久失效。索引丢失/算错不是假想：

- 迁移 a4b5c6d7e8f9 的回填带 ``NOT LIKE 'pii1$%'``：库已加密后重跑（回滚重来、
  换库重建），密文行**全部被跳过**，索引列留一片 NULL；
- 部署 shell 里忘了导 MEDPLAT_SECRET 就跑 ``alembic upgrade heads``：迁移用
  默认 secret 算 HMAC，应用带真密钥启动——索引**全部非空但全部算错**。
  （该情形现已被迁移里的默认密钥守卫拦下，但存量库仍需要本模式修回。）

两种情形的共同特征是**零报错零日志**：查不到只表现为"这人没建过档"，
于是重复建档、重复开户。本模式就是它们唯一的修复手段：

    python scripts/pii_encrypt_backfill.py --rebuild-index [--old-secret 旧值]

解密按 当前 secret → MEDPLAT_SECRET_PREVIOUS → --old-secret 依次尝试，
索引一律按**当前** secret 重算（写入侧只认当前钥，与 app/pii.py 同口径）。
明文行同样重算（关态/混存期的索引也要铺满）。幂等：索引已等于目标值的行跳过，
所以修完重跑是 0 改写。

## 安全性

- **幂等**：无 --old-secret 时带 pii1$ 前缀的行直接跳过，中断重跑无害；
  --rebuild-index 模式按"索引已正确则跳过"幂等；
- **回读校验**：每行先 decrypt(加密结果) == 原值 才提交，算错宁可不写；
- **分批 commit**（--batch-size，默认 500）：失败只回滚当前批；
- --dry-run 只统计不落库。

用法：
    cd server
    python scripts/pii_encrypt_backfill.py [--dry-run] [--batch-size 500]
        [--old-secret 旧MEDPLAT_SECRET] [--rebuild-index]

数据库连接沿用 MEDPLAT_DATABASE_URL（与应用一致）。退出码：0=成功；1=有失败行。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.database import engine  # noqa: E402
from app.pii import PII_PREFIX, decrypt_pii, encrypt_pii, pii_index  # noqa: E402

#: (表, 明文列, 索引列)
TARGETS = [
    ("patients", "id_card", "id_card_idx"),
    ("patients", "phone", "phone_idx"),
    ("resident_accounts", "phone", "phone_idx"),
]


def _iter_batches(conn, table: str, column: str, after_id: int, limit: int):
    return conn.execute(
        text(
            f"SELECT id, {column} AS value FROM {table} "  # noqa: S608 - 表列名来自本文件常量
            f"WHERE id > :after AND {column} IS NOT NULL AND {column} != '' "
            f"ORDER BY id LIMIT :limit"
        ),
        {"after": after_id, "limit": limit},
    ).fetchall()


def backfill_column(
    table: str, column: str, idx_column: str, *,
    batch_size: int = 500, dry_run: bool = False, old_secret: str | None = None,
) -> tuple[int, int, int]:
    """返回 (改写行数, 跳过行数, 失败行数)。按 id 游标分批、每批一个事务。"""
    rewritten = skipped = failed = 0
    after_id = 0
    while True:
        with engine.begin() as conn:
            rows = _iter_batches(conn, table, column, after_id, batch_size)
            if not rows:
                break
            updates = []
            for row in rows:
                after_id = row.id
                value = str(row.value)
                if value.startswith(PII_PREFIX):
                    if old_secret is None:
                        skipped += 1  # 已是密文：幂等跳过
                        continue
                    try:  # 轮换重加密：旧钥解开 → 当前钥重加密
                        value = decrypt_pii(value, secret=old_secret)
                    except ValueError:
                        try:  # 已是当前钥的密文（脚本重跑）：跳过
                            decrypt_pii(value)
                            skipped += 1
                            continue
                        except ValueError:
                            print(f"[失败] {table}.{column} id={row.id}: 新旧密钥均无法解密")
                            failed += 1
                            continue
                stored = encrypt_pii(value)
                if decrypt_pii(stored) != value:  # 回读校验：算错宁可不写
                    print(f"[失败] {table}.{column} id={row.id}: 回读校验不一致")
                    failed += 1
                    continue
                updates.append({"id": row.id, "v": stored, "idx": pii_index(value)})
            if updates and not dry_run:
                conn.execute(
                    text(
                        f"UPDATE {table} SET {column} = :v, {idx_column} = :idx "  # noqa: S608
                        f"WHERE id = :id"
                    ),
                    updates,
                )
            rewritten += len(updates)
    return rewritten, skipped, failed


def _decrypt_any(stored: str, old_secret: str | None) -> str:
    """按 当前 secret → secret_previous → --old-secret 依次尝试解密，全败则抛。

    前两把是 `decrypt_pii` 的内建回退；`--old-secret` 是多出来的一把：轮换宽限期
    没配 MEDPLAT_SECRET_PREVIOUS、或旧钥已从环境里摘掉时，运维还能从命令行传进来。
    """
    candidates = [None]  # None = decrypt_pii 的默认候选（当前钥 + secret_previous）
    if old_secret:
        candidates.append(old_secret)
    last: ValueError | None = None
    for cand in candidates:
        try:
            return decrypt_pii(stored, secret=cand) if cand else decrypt_pii(stored)
        except ValueError as exc:
            last = exc
    raise last if last else ValueError("无可用密钥")


def rebuild_index_column(
    table: str, column: str, idx_column: str, *,
    batch_size: int = 500, dry_run: bool = False, old_secret: str | None = None,
) -> tuple[int, int, int]:
    """只重算 `*_idx`、不改密文列。返回 (重算行数, 跳过行数, 失败行数)。

    密文行解密后按**当前** secret 重算索引；明文行直接算。索引已等于目标值的
    行跳过（幂等）。分批、可 dry-run，与 `backfill_column` 同一套边界。
    """
    rebuilt = skipped = failed = 0
    after_id = 0
    while True:
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    f"SELECT id, {column} AS value, {idx_column} AS idx FROM {table} "  # noqa: S608 - 表列名来自本文件常量
                    f"WHERE id > :after AND {column} IS NOT NULL AND {column} != '' "
                    f"ORDER BY id LIMIT :limit"
                ),
                {"after": after_id, "limit": batch_size},
            ).fetchall()
            if not rows:
                break
            updates = []
            for row in rows:
                after_id = row.id
                stored = str(row.value)
                if stored.startswith(PII_PREFIX):
                    try:
                        plain = _decrypt_any(stored, old_secret)
                    except ValueError:
                        print(f"[失败] {table}.{column} id={row.id}: 无可用密钥解密，索引无法重算")
                        failed += 1
                        continue
                else:
                    plain = stored
                target = pii_index(plain)
                if row.idx == target:
                    skipped += 1  # 索引已正确：幂等跳过
                    continue
                updates.append({"id": row.id, "idx": target})
            if updates and not dry_run:
                conn.execute(
                    text(f"UPDATE {table} SET {idx_column} = :idx WHERE id = :id"),  # noqa: S608
                    updates,
                )
            rebuilt += len(updates)
    return rebuilt, skipped, failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="只统计不落库")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--old-secret", default=None,
        help="密钥轮换：旧 MEDPLAT_SECRET——旧钥密文解开后按当前钥重加密并重算索引",
    )
    parser.add_argument(
        "--rebuild-index", action="store_true",
        help="只重算 *_idx 检索索引（不改密文列）：索引丢失/算错后的唯一修复手段",
    )
    args = parser.parse_args()

    total_failed = 0
    for table, column, idx_column in TARGETS:
        runner = rebuild_index_column if args.rebuild_index else backfill_column
        changed, skipped, failed = runner(
            table, column, idx_column,
            batch_size=args.batch_size, dry_run=args.dry_run, old_secret=args.old_secret,
        )
        total_failed += failed
        mode = "dry-run，未落库" if args.dry_run else "已提交"
        verb = "重算索引" if args.rebuild_index else "改写"
        print(f"{table}.{column}: {verb} {changed} 行，跳过 {skipped} 行，失败 {failed} 行（{mode}）")
    return 1 if total_failed else 0


if __name__ == "__main__":
    sys.exit(main())
