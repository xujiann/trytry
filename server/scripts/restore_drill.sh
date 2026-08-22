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
if [ -f "$ARCHIVE.sha256" ]; then
  # 与 restore.sh 同口径：只认哈希值，路径按当前这个包算（校验文件里可能记着
  # 备份当时的相对路径，换个 cwd 就找不到文件）。
  EXPECTED_SUM="$(awk '{print $1; exit}' "$ARCHIVE.sha256")"
  ACTUAL_SUM="$(cd "$(dirname "$ARCHIVE")" && sha256sum "$(basename "$ARCHIVE")" | awk '{print $1}')"
  [ "$EXPECTED_SUM" = "$ACTUAL_SUM" ] || {
    echo "备份包校验失败：期望 $EXPECTED_SUM，实际 $ACTUAL_SUM" >&2
    exit 4
  }
  echo "完整性校验通过：$ACTUAL_SUM"
fi
tar -xzf "$ARCHIVE" -C "$DRILL_DIR"

if [ -f "$DRILL_DIR/manifest.txt" ]; then
  echo "备份清单："
  sed 's/^/  /' "$DRILL_DIR/manifest.txt"
fi

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

print("[1/4] 库能打开、表数正常")
tables = cur.execute(
    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
).fetchone()[0]
print(f"  表数：{tables}")
if tables <= 100:
    sys.exit("表数异常偏少，备份可能不完整")

print("[2/4] 关键表有数据")
for table in ("users", "organizations"):
    n = cur.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    print(f"  {table}：{n} 行")
    if n == 0:
        sys.exit(f"  {table} 为空，备份可疑")

print("[3/4] 迁移版本可识别")
rows = cur.execute("SELECT version_num FROM alembic_version").fetchall()
print(f"  alembic 版本：{[r[0] for r in rows] or '缺失'}")
if not rows:
    sys.exit("alembic_version 为空，恢复后无法确定代码版本")

print("[4/4] 双 head 齐全（平台链 + spd 链）")
# 本仓库是双分支迁移：alembic_version 应当**恰有两行**。
# 原先只 fetchone() 看一行——少了 spd 链的备份（59 张 spd_ 表全无）
# 照样能"演练通过"，这正是 fetchone 这类"看一眼就下结论"的检查最典型的失效方式。
print(f"  head 行数：{len(rows)}（预期 2）")
if len(rows) != 2:
    sys.exit(
        f"alembic_version 有 {len(rows)} 行，预期 2 行（平台链 + spd 链）："
        " 备份可能来自单 head 升级（alembic upgrade head 而非 heads），spd 子系统的表会缺失"
    )

print("[覆盖面] 本次演练检查了：表数 / users+organizations 行数 / 迁移版本行数（双 head）；")
print("[覆盖面] 未检查：附件目录内容、密钥指纹、业务不变量——这三项由 restore.sh 与人工核对承担。")
PYCHECK

echo "演练通过。请把本次结果留档（时间、备份包、表数、版本号）——"
echo "留档的意义在于：出事那天要拿得出'上一次演练是什么时候、结果如何'。"
