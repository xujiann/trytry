#!/usr/bin/env bash
# PG 连续归档备份封装（工程包 P2：WAL 归档 + PITR）。
#
# backup.sh 的每日全量 pg_dump 是逻辑备份，RPO≈24 小时；对 RPO≤15min 的县，
# 用 pgBackRest 做物理备份 + WAL 连续归档。本脚本只封装常用动作，
# 策略（保留期/加密/目录）都在 pgbackrest.conf 里（样例见同目录
# pgbackrest.conf.example，含 postgresql.conf 的 archive_command 配置）。
#
# 用法：scripts/backup_pg_pitr.sh <full|diff|incr|check|info> [stanza]
#   full   全量物理备份（建议每周）
#   diff   差异备份（相对最近全量；建议每日）
#   incr   增量备份（相对最近任意备份；可加密到小时级）
#   check  校验归档链路（archive_command 是否真的在推 WAL）
#   info   查看备份集与可恢复区间
# 建议 crontab：
#   0 1 * * 0  scripts/backup_pg_pitr.sh full
#   0 1 * * 1-6 scripts/backup_pg_pitr.sh diff
# RPO 由 WAL 归档保证（archive_timeout 兜底），不靠备份频率。
set -euo pipefail

MODE="${1:-}"
STANZA="${2:-medplat}"

if ! command -v pgbackrest >/dev/null 2>&1; then
  cat >&2 <<'EOF'
错误：未检测到 pgbackrest，WAL 归档/PITR 依赖它。安装指引：
  Debian/Ubuntu : apt-get install pgbackrest
  RHEL/openEuler: dnf install pgbackrest   （需 PGDG 源）
安装后：
  1) 按 scripts/pgbackrest.conf.example 写 /etc/pgbackrest/pgbackrest.conf；
  2) postgresql.conf 开启 archive_mode=on 与
     archive_command='pgbackrest --stanza=medplat archive-push %p'，重启 PG；
  3) sudo -u postgres pgbackrest --stanza=medplat stanza-create
  4) sudo -u postgres pgbackrest --stanza=medplat check
EOF
  exit 3
fi

case "$MODE" in
  full|diff|incr)
    pgbackrest --stanza="$STANZA" --type="$MODE" backup
    pgbackrest --stanza="$STANZA" info
    ;;
  check)
    pgbackrest --stanza="$STANZA" check
    ;;
  info)
    pgbackrest --stanza="$STANZA" info
    ;;
  *)
    echo "用法：$0 <full|diff|incr|check|info> [stanza，默认 medplat]" >&2
    exit 2
    ;;
esac

echo "提醒：备份能不能用，只有恢复的时候才知道——请按运维手册周期跑 scripts/restore_drill_pg.sh"
