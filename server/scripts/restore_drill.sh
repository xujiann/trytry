#!/usr/bin/env bash
# 恢复演练（阶段十三）：在临时目录里把备份恢复一遍并做基本校验。
#
# **备份能不能用，只有恢复的时候才知道，而那时候来不及了。**
# 这个脚本的意义就是把"那时候"提前到平时，且不碰生产数据。
#
# 用法：scripts/restore_drill.sh <备份包路径>
set -euo pipefail

ARCHIVE="${1:?用法：scripts/restore_drill.sh <备份包路径>}"
DRILL_DIR="$(mktemp -d)"
trap 'rm -rf "$DRILL_DIR"' EXIT

echo "演练目录：$DRILL_DIR（生产数据不受影响）"
tar -xzf "$ARCHIVE" -C "$DRILL_DIR"

if [ ! -f "$DRILL_DIR/database.sqlite" ]; then
  echo "本脚本目前只演练 SQLite 备份；PostgreSQL 请恢复到独立演练库后跑同样的校验。" >&2
  exit 2
fi

# 走 python3 而不是 sqlite3 CLI：平台本来就跑在 Python 上，而 sqlite3 命令行在
# 精简容器镜像里经常没装——演练脚本因为缺个 CLI 跑不起来，是最没必要的失败。
python3 - "$DRILL_DIR/database.sqlite" <<'PYCHECK'
import sqlite3
import sys

cur = sqlite3.connect(sys.argv[1]).cursor()

print("[1/3] 库能打开、表数正常")
tables = cur.execute(
    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
).fetchone()[0]
print(f"  表数：{tables}")
if tables <= 100:
    sys.exit("表数异常偏少，备份可能不完整")

print("[2/3] 关键表有数据")
for table in ("users", "organizations"):
    n = cur.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    print(f"  {table}：{n} 行")
    if n == 0:
        sys.exit(f"  {table} 为空，备份可疑")

print("[3/3] 迁移版本可识别")
row = cur.execute("SELECT version_num FROM alembic_version").fetchone()
print(f"  alembic 版本：{row[0] if row else '缺失'}")
if not row:
    sys.exit("alembic_version 为空，恢复后无法确定代码版本")
PYCHECK

echo "演练通过。请把本次结果留档（时间、备份包、表数、版本号）——"
echo "留档的意义在于：出事那天要拿得出'上一次演练是什么时候、结果如何'。"
