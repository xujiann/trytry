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
  # 一律切到备份包所在目录、按 basename 校验：老备份包的 .sha256 里可能写着
  # 备份当时的相对路径（如 ./backups/xxx.tar.gz），在别的 cwd 下 `-c` 会报
  # "No such file or directory"，`set -e` 当场退出——恢复在第 0 步就死了。
  # 只取校验文件里的哈希值本身，路径以当前这个包为准。
  EXPECTED_SUM="$(awk '{print $1; exit}' "$ARCHIVE.sha256")"
  ACTUAL_SUM="$(cd "$(dirname "$ARCHIVE")" && sha256sum "$(basename "$ARCHIVE")" | awk '{print $1}')"
  if [ "$EXPECTED_SUM" != "$ACTUAL_SUM" ]; then
    echo "备份包校验失败：期望 $EXPECTED_SUM，实际 $ACTUAL_SUM" >&2
    echo "包已损坏或被替换，恢复中止。" >&2
    exit 4
  fi
  echo "  校验通过：$ACTUAL_SUM"
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
# 包里的顶层目录名是**备份时**那台机器的 upload_dir basename。原先直接解到
# `dirname(当前 UPLOAD_DIR)`，于是换机恢复（备份时 uploads/、现在 /data/attachments）
# 会把附件还原成 .../uploads/ 这个**没人读的目录**：脚本报"完成"，应用看到 0 个附件。
# 改为先解到临时目录，再把内容搬进当前 MEDPLAT_UPLOAD_DIR，名字对不上时告警。
if [ -f "$WORK/uploads.tar.gz" ]; then
  BACKUP_UPLOAD_BASENAME="$(sed -n 's/^upload_dir_basename=//p' "$WORK/manifest.txt" | head -1)"
  STAGE="$WORK/uploads-stage"
  mkdir -p "$STAGE"
  tar -xzf "$WORK/uploads.tar.gz" -C "$STAGE"
  SRC="$(find "$STAGE" -mindepth 1 -maxdepth 1 -type d | head -1)"
  if [ -z "$SRC" ]; then
    echo "  附件包里没有顶层目录，跳过（包可能损坏）" >&2
  else
    if [ -n "$BACKUP_UPLOAD_BASENAME" ] && [ "$BACKUP_UPLOAD_BASENAME" != "$(basename "$UPLOAD_DIR")" ]; then
      echo "  提示：备份时附件目录名为 $BACKUP_UPLOAD_BASENAME，当前 MEDPLAT_UPLOAD_DIR=$UPLOAD_DIR，已按当前配置落盘"
    fi
    mkdir -p "$UPLOAD_DIR"
    # -T：把 SRC 目录**里面的内容**搬进目标目录，而不是搬成 $UPLOAD_DIR/uploads
    cp -a -T "$SRC" "$UPLOAD_DIR"
    RESTORED="$(find "$UPLOAD_DIR" -type f | wc -l | tr -d ' ')"
    echo "  附件已恢复到 $UPLOAD_DIR（$RESTORED 个文件）"
    if [ "$RESTORED" = "0" ]; then
      echo "  警告：恢复后附件数为 0，请核对备份包与 MEDPLAT_UPLOAD_DIR" >&2
    fi
  fi
fi

echo "[4/4] 迁移到当前代码版本"
cd "$(dirname "$0")/.."
# 复数 heads：本仓库双 head（平台链 + spd 链），单数 head 会报错并漏掉 spd 的表。
alembic upgrade heads
CURRENT_HEADS="$(alembic heads 2>/dev/null | sed -E 's/ *\(.*\)//' | grep -c . || true)"
if [ "$CURRENT_HEADS" != "2" ]; then
  echo "警告：当前代码的 alembic head 数为 $CURRENT_HEADS（预期 2：平台链 + spd 链）" >&2
fi

echo "完成。请登录后访问 /api/audit/verify 确认审计链完好。"
