#!/usr/bin/env bash
# 从备份包恢复（阶段十三）。
#
# 恢复前校验密钥指纹：不一致就停下来问，不静默恢复——密钥变了，
# 审计哈希链会全部验不过，而那时人们通常会误以为是"审计被篡改了"。
#
# 用法：scripts/restore.sh <备份包路径> [--force]
set -euo pipefail

ARCHIVE="${1:?用法：scripts/restore.sh <备份包路径> [--force]}"
FORCE="${2:-}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [ -f "$ARCHIVE.sha256" ]; then
  echo "[0/4] 校验备份包完整性"
  sha256sum -c "$ARCHIVE.sha256"
fi

tar -xzf "$ARCHIVE" -C "$WORK"
cat "$WORK/manifest.txt"

DB_URL="${MEDPLAT_DATABASE_URL:-sqlite:///./medplat.db}"
UPLOAD_DIR="${MEDPLAT_UPLOAD_DIR:-uploads}"
SECRET="${MEDPLAT_SECRET:-}"

echo "[1/4] 校验密钥指纹"
EXPECTED="$(cat "$WORK/secret.fingerprint")"
ACTUAL="$(printf '%s' "$SECRET" | sha256sum | cut -d' ' -f1)"
if [ "$EXPECTED" != "unset" ] && [ "$EXPECTED" != "$ACTUAL" ]; then
  echo "密钥与备份时不一致：恢复后审计哈希链将无法校验（历史记录会全部报断链）。" >&2
  if [ "$FORCE" != "--force" ]; then
    echo "确认要继续请加 --force。" >&2
    exit 3
  fi
  echo "已按 --force 继续。" >&2
fi

echo "[2/4] 恢复数据库"
case "$DB_URL" in
  sqlite*)
    DB_PATH="${DB_URL#sqlite:///}"
    [ -f "$DB_PATH" ] && mv "$DB_PATH" "$DB_PATH.before-restore-$(date -u +%s)"
    cp "$WORK/database.sqlite" "$DB_PATH"
    ;;
  postgresql*)
    pg_restore --clean --if-exists -d "$DB_URL" "$WORK/database.dump"
    ;;
esac

echo "[3/4] 恢复附件目录"
if [ -f "$WORK/uploads.tar.gz" ]; then
  mkdir -p "$(dirname "$UPLOAD_DIR")"
  tar -xzf "$WORK/uploads.tar.gz" -C "$(dirname "$UPLOAD_DIR")"
fi

echo "[4/4] 迁移到当前代码版本"
cd "$(dirname "$0")/.."
alembic upgrade heads

echo "完成。请登录后访问 /api/audit/verify 确认审计链完好。"
