#!/bin/sh
# 数据备份脚本：对 docker-compose 中的 PostgreSQL 执行 pg_dump，按日期落盘。
# 用法：sh scripts/backup.sh [备份目录]
# 建议 crontab 定时：0 2 * * * sh /path/to/scripts/backup.sh /data/backups
set -eu

BACKUP_DIR="${1:-./backups}"
STAMP="$(date +%Y%m%d_%H%M%S)"
DB_SERVICE="${MEDPLAT_DB_SERVICE:-db}"
DB_USER="${MEDPLAT_DB_USER:-medplat}"
DB_NAME="${MEDPLAT_DB_NAME:-medplat}"
KEEP_DAYS="${MEDPLAT_BACKUP_KEEP_DAYS:-30}"

mkdir -p "$BACKUP_DIR"
OUT="$BACKUP_DIR/medplat_${STAMP}.sql.gz"

docker compose exec -T "$DB_SERVICE" pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$OUT"
echo "备份完成：$OUT"

# 清理超过保留天数的旧备份
find "$BACKUP_DIR" -name 'medplat_*.sql.gz' -mtime +"$KEEP_DAYS" -delete
