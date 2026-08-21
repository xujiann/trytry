#!/usr/bin/env bash
# PG 恢复演练（工程包 P2）：从最近一次 pgBackRest 备份恢复到**临时实例**，
# 跑 `alembic current` + 健康查询后销毁。不碰生产数据目录与生产端口。
#
# 用法：scripts/restore_drill_pg.sh [--dry-run] [stanza，默认 medplat]
# 环境变量：
#   DRILL_PORT    临时实例端口（默认 54329，避开生产 5432）
#   DRILL_DIR     临时数据目录（默认 mktemp -d；演练结束删除）
#   DRILL_DB      演练健康查询用库名（默认 medplat）
#   DRILL_DB_URL  覆盖 alembic/健康查询使用的连接串
#                 （默认 postgresql://postgres@127.0.0.1:$DRILL_PORT/$DRILL_DB）
#   PG_BIN        pg_ctl 所在目录（默认 PATH 里找；发行版装在
#                 /usr/lib/postgresql/16/bin 等处时需指定）
#
# 无 PG 环境（缺 pgbackrest 或 pg_ctl）或传 --dry-run 时进入 dry-run：
# 只打印将执行的步骤并成功退出，供在办公机上评审流程；真实演练必须在
# 装有 pgBackRest 仓库访问权的 PG 机器上跑。
set -euo pipefail

DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN=1
  shift
fi
STANZA="${1:-medplat}"
DRILL_PORT="${DRILL_PORT:-54329}"
DRILL_DB="${DRILL_DB:-medplat}"

PG_CTL="pg_ctl"
if [ -n "${PG_BIN:-}" ]; then
  PG_CTL="$PG_BIN/pg_ctl"
fi

if ! command -v pgbackrest >/dev/null 2>&1 || ! command -v "$PG_CTL" >/dev/null 2>&1; then
  if [ "$DRY_RUN" -eq 0 ]; then
    echo "未检测到 pgbackrest / pg_ctl（PG_BIN 可指定 bin 目录），转入 dry-run 打印步骤。" >&2
    echo "真实演练请在 PG 服务器上执行；安装指引见 scripts/backup_pg_pitr.sh。" >&2
    DRY_RUN=1
  fi
fi

DRILL_DIR="${DRILL_DIR:-}"
if [ "$DRY_RUN" -eq 0 ] && [ -z "$DRILL_DIR" ]; then
  DRILL_DIR="$(mktemp -d /tmp/medplat-restore-drill.XXXXXX)"
fi
: "${DRILL_DIR:=/tmp/medplat-restore-drill.XXXXXX}"
DRILL_DB_URL="${DRILL_DB_URL:-postgresql://postgres@127.0.0.1:$DRILL_PORT/$DRILL_DB}"

run() {
  # dry-run 只打印；真跑先打印再执行（演练留痕本来就该看得见每一步）
  echo "+ $*"
  if [ "$DRY_RUN" -eq 0 ]; then
    "$@"
  fi
}

echo "== PG 恢复演练（stanza=$STANZA, 临时目录=$DRILL_DIR, 端口=$DRILL_PORT, dry-run=$DRY_RUN) =="

echo "-- [1/5] 从最近一次备份恢复（含重放已归档 WAL 到最新一致点）"
run pgbackrest --stanza="$STANZA" --pg1-path="$DRILL_DIR" restore
# 提示：定点恢复（PITR）在真实事故时加 --type=time --target='YYYY-MM-DD HH:MM:SS'

echo "-- [2/5] 以临时端口启动恢复出的实例（不监听外网、不与生产抢端口）"
run "$PG_CTL" -D "$DRILL_DIR" -o "-p $DRILL_PORT -c listen_addresses=127.0.0.1 -c archive_mode=off" -w start

echo "-- [3/5] 迁移版本校验：alembic current 必须能连上并给出版本号"
run env MEDPLAT_DATABASE_URL="$DRILL_DB_URL" alembic -c "$(dirname "$0")/../alembic.ini" current

echo "-- [4/5] 健康查询：核心表可读、行数非负（抽查患者/用户/审计）"
run psql "$DRILL_DB_URL" -At \
  -c "SELECT 'users', count(*) FROM users;" \
  -c "SELECT 'patients', count(*) FROM patients;" \
  -c "SELECT 'audit_logs', count(*) FROM audit_logs;"

echo "-- [5/5] 停止临时实例并清理数据目录"
run "$PG_CTL" -D "$DRILL_DIR" -w stop
run rm -rf "$DRILL_DIR"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "dry-run 结束：以上为演练将执行的步骤，未发生任何实际操作。"
else
  echo "演练完成：恢复、迁移版本与健康查询均通过。请把结果记入演练台账（运维手册第四节）。"
fi
