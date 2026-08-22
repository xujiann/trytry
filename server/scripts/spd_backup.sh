#!/usr/bin/env bash
# 慢专病子系统的**分子系统**备份：只导 spd_* 表。
#
# 与 scripts/backup.sh 的关系：那个是平台全量备份，是灾备的正解，**不能替代**。
# 这个解决的是另外三件事：
#
# 1. 子系统单独升级前的回滚点——只回滚慢专病，不动平台其余业务；
# 2. 把某个县的慢专病数据交付给上级卫健或第三方评估，不必连带全平台数据；
# 3. 迁库演练时只搬子系统，验证"表前缀是真的边界"而不只是命名习惯。
#
# **注意**：这份导出**不是自洽的**。spd_* 表有 76 个外键指向 users /
# organizations / patients，单独恢复到一个空库会因为找不到主数据而失败。
# 它的正确用法是恢复到**同一套平台数据**上（同一个 patients/organizations 快照）。
# 这一点写在这里而不是留给使用者猜——"以为备份了其实恢复不了"是备份最常见的坑。
#
# 用法：scripts/spd_backup.sh [输出目录]（默认 ./backups）
set -euo pipefail

OUT_DIR="${1:-./backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DB_URL="${MEDPLAT_DATABASE_URL:-sqlite:///./medplat.db}"
PREFIX="spd_"

mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/medplat-spd-$STAMP.sql"

echo "子系统备份：$DB_URL（仅 ${PREFIX}* 表）"
case "$DB_URL" in
  sqlite*)
    DB_PATH="${DB_URL#sqlite:///}"
    python3 - "$DB_PATH" "$PREFIX" > "$OUT" <<'PYDUMP'
import sqlite3
import sys

path, prefix = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(path)
names = [
    row[0]
    for row in conn.execute(
        "select name from sqlite_master where type='table' and name like ?", (prefix + "%",)
    )
]
if not names:
    print("-- 没有找到子系统表，请确认连接串与迁移是否已执行", file=sys.stderr)
    raise SystemExit(2)
print(f"-- medplat 慢专病子系统导出：{len(names)} 张表")
print("-- 恢复前提：目标库已有同一套 users / organizations / patients 主数据")
for line in conn.iterdump():
    # iterdump 会导出全库，这里按表名过滤；事务语句保留
    if line.startswith(("BEGIN", "COMMIT")):
        print(line)
        continue
    if any(f'"{name}"' in line or f" {name} " in line for name in names):
        print(line)
PYDUMP
    ;;
  postgresql*)
    # -t 支持通配；--data-only 与 --schema-only 视用途选择，这里连结构一起导
    pg_dump "$DB_URL" -t "${PREFIX}*" -f "$OUT"
    ;;
  *)
    echo "不认识的数据库类型，请按 docs/信创适配与备份容灾.md 补充导出命令" >&2
    exit 2
    ;;
esac

# 与 backup.sh 同口径：校验文件里只写 basename，换 cwd 也能 `sha256sum -c`。
(cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")") > "$OUT.sha256"
TABLES=$(grep -c "^CREATE TABLE" "$OUT" || true)
echo "完成：$OUT（$TABLES 张表）"
echo "提醒：本导出依赖平台主数据，恢复目标库必须已有对应的 users/organizations/patients"
