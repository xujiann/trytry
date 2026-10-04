#!/usr/bin/env bash
# PG 恢复演练（工程包 P2）：从最近一次 pgBackRest 备份恢复到**临时实例**，
# 跑 `alembic current` + 健康查询后销毁。不碰生产数据目录与生产端口。
# 中途哪一步失败也照样停临时实例、删恢复出的数据目录再退出，退出码照旧非 0（P2-1269）。
#
# 用法：scripts/restore_drill_pg.sh [--dry-run] [stanza，默认 medplat]
# 环境变量：
#   DRILL_PORT    临时实例端口（默认 54329，避开生产 5432）
#   DRILL_DIR     临时数据目录（默认 mktemp -d；演练结束或中途失败都删除——
#                 指定的目录若本来就有东西、第 1 步没恢复进去，失败收场时不碰）
#   DRILL_DB      演练健康查询用库名（默认 medplat）
#   DRILL_DB_URL  覆盖 alembic/健康查询使用的连接串
#                 （默认 postgresql://postgres@127.0.0.1:$DRILL_PORT/$DRILL_DB）
#   PG_BIN        pg_ctl 所在目录（默认 PATH 里找；发行版装在
#                 /usr/lib/postgresql/16/bin 等处时需指定）
#
# 无 PG 环境（缺 pgbackrest 或 pg_ctl）或传 --dry-run 时进入 dry-run：
# 只打印将执行的步骤并成功退出，供在办公机上评审流程；真实演练必须在
# 装有 pgBackRest 仓库访问权的 PG 机器上跑。
# 用 `sh 脚本` 调用（运维手册与 crontab 的写法）时 sh 无视 shebang：Debian 系的 /bin/sh 是 dash，下一行的 pipefail
# 它执行不了、当场退出，备份 / 恢复一次都没跑成（P1-234）。不是 bash 就换 bash 重新执行自己
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
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

# 中途失败也得收场（P2-1269）：恢复出来的是明文整库（pgbackrest.conf.example 写明备份含居民敏感信息、仓库必须加密，
# 恢复出的数据目录却是明文），临时实例占着 DRILL_PORT。原先停实例与 rm -rf 只在第 5 步：第 2～4 步任一步失败（实例
# 起不来、alembic 连不上或版本不对、psql 鉴权不过）set -e 当场退出，整库留在 /tmp、实例接着监听，下次演练用同一个
# 默认端口又在第 2 步失败、再留一份。照同目录 restore_drill.sh / backup.sh / restore.sh 用 EXIT trap 收场，几处讲究：
# - 先存下退出码、关掉 set -e 再动手：trap 里一步失败若照 set -e 中止，后面的 rm -rf 就不跑了，退出码也被它盖掉；
#   停不下来照样删目录，最后按原退出码退出（失败就是失败）；
# - 只删这次演练恢复出来的东西：恢复前目录不存在或是空的（mktemp 建的、或调用方给的空目录），或第 1 步已往里恢复
#   成功（第 5 步本来就要删它）。调用方给的 DRILL_DIR 本来就有东西时（指错了目录——pgbackrest 默认拒绝往非空目录里
#   恢复，第 1 步就失败），收场一个字节都不碰；
# - 第 5 步正常走完时实例已停、目录已删，这里什么都不做；dry-run 不装（只打印、不动任何东西）。
DRILL_PG_STARTED=0
DRILL_DIR_OURS=0
cleanup_drill() {
  rc=$?
  set +e
  if [ "$DRILL_PG_STARTED" -eq 1 ]; then
    echo "-- 演练中途退出（退出码 $rc）：停止临时实例" >&2
    run "$PG_CTL" -D "$DRILL_DIR" -w -m fast stop || echo "   临时实例没停下来（也可能根本没起来），照样删除数据目录" >&2
  fi
  if [ "$DRILL_DIR_OURS" -eq 1 ] && [ -d "$DRILL_DIR" ]; then
    echo "-- 演练中途退出（退出码 $rc）：删除恢复出的数据目录，明文整库不留在磁盘上" >&2
    run rm -rf "$DRILL_DIR" || echo "   数据目录没删掉，内含恢复出的明文整库，请手工删除：$DRILL_DIR" >&2
  fi
  exit "$rc"
}
if [ "$DRY_RUN" -eq 0 ]; then
  if [ ! -e "$DRILL_DIR" ] || { [ -d "$DRILL_DIR" ] && [ -z "$(ls -A "$DRILL_DIR")" ]; }; then
    DRILL_DIR_OURS=1
  fi
  trap cleanup_drill EXIT
fi

echo "== PG 恢复演练（stanza=$STANZA, 临时目录=$DRILL_DIR, 端口=$DRILL_PORT, dry-run=$DRY_RUN) =="

echo "-- [1/5] 从最近一次备份恢复（含重放已归档 WAL 到最新一致点）"
run pgbackrest --stanza="$STANZA" --pg1-path="$DRILL_DIR" restore
DRILL_DIR_OURS=1   # 恢复成功：目录里就是恢复出的整库，中途失败时归收场删（P2-1269）
# 提示：定点恢复（PITR）在真实事故时加 --type=time --target='YYYY-MM-DD HH:MM:SS'

echo "-- [2/5] 以临时端口启动恢复出的实例（不监听外网、不与生产抢端口）"
# 先置位再 start（P2-1269）：`pg_ctl -w start` 等满 PGCTLTIMEOUT（默认 60 秒）实例还没就绪就返回非 0，postmaster 却
# 照样在后台重放 WAL、监听端口——恢复出的库 WAL 一多就是这一种；真没起来时收场那次 stop 失败也无妨
DRILL_PG_STARTED=1
run "$PG_CTL" -D "$DRILL_DIR" -o "-p $DRILL_PORT -c listen_addresses=127.0.0.1 -c archive_mode=off" -w start

echo "-- [3/5] 迁移版本校验：alembic current 必须能连上并给出版本号"
run env MEDPLAT_DATABASE_URL="$DRILL_DB_URL" alembic -c "$(dirname "$0")/../alembic.ini" current

echo "-- [4/5] 健康查询：核心表可读、行数非负（抽查患者/用户/审计）"
run psql "postgresql://${DRILL_DB_URL#*://}" -At \
  -c "SELECT 'users', count(*) FROM users;" \
  -c "SELECT 'patients', count(*) FROM patients;" \
  -c "SELECT 'audit_logs', count(*) FROM audit_logs;"

echo "-- [5/5] 停止临时实例并清理数据目录"
run "$PG_CTL" -D "$DRILL_DIR" -w stop
DRILL_PG_STARTED=0
run rm -rf "$DRILL_DIR"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "dry-run 结束：以上为演练将执行的步骤，未发生任何实际操作。"
else
  echo "演练完成：恢复、迁移版本与健康查询均通过。请把结果记入演练台账（运维手册第四节）。"
fi
