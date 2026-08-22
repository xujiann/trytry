#!/usr/bin/env bash
# 平台全量备份（阶段十三）。
#
# 平台的状态分三处，必须一起备、按同一时点备：数据库、附件目录、平台密钥。
# 三者分开备份的后果各不相同，最隐蔽的是密钥——恢复后审计哈希链再也验不了。
#
# 密钥**不打进备份包**（打进去等于把锁和钥匙放一个盒子），只记指纹，
# 恢复时比对；不一致就明确告警，不静默恢复。
#
# 用法：scripts/backup.sh [输出目录]（默认 ./backups）
set -euo pipefail

OUT_DIR="${1:-./backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

DB_URL="${MEDPLAT_DATABASE_URL:-sqlite:///./medplat.db}"
UPLOAD_DIR="${MEDPLAT_UPLOAD_DIR:-uploads}"
SECRET="${MEDPLAT_SECRET:-}"

mkdir -p "$OUT_DIR"

echo "[1/4] 备份数据库：$DB_URL"
case "$DB_URL" in
  sqlite*)
    DB_PATH="${DB_URL#sqlite:///}"
    # 库文件必须**先存在**才能备。sqlite3.connect() 遇到不存在的路径会**新建一个空库**，
    # 于是连接串写错（少一个斜杠、相对路径跑错了 cwd）时备份照样 exit 0，
    # 备出来的是一个 0 表的空库——出事那天才发现"备份一直在跑、里面什么都没有"。
    if [ ! -f "$DB_PATH" ]; then
      echo "SQLite 库文件不存在：$DB_PATH（来自 MEDPLAT_DATABASE_URL=$DB_URL）" >&2
      echo "请确认连接串与当前工作目录；备份不会替你新建空库。" >&2
      exit 2
    fi
    # 用 sqlite 的在线备份 API 而不是 cp：cp 会在写入过程中拿到撕裂的文件。
    # 走 python3 而不是 sqlite3 命令行——平台本来就跑在 Python 上，
    # 而 sqlite3 CLI 在精简的容器镜像里经常没装，演练脚本因为缺个 CLI 跑不起来
    # 是最没必要的失败。
    python3 - "$DB_PATH" "$WORK/database.sqlite" <<'PYBACKUP'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as src, sqlite3.connect(sys.argv[2]) as dst:
    src.backup(dst)
PYBACKUP
    ;;
  postgresql*)
    # -Fc 自定义格式：支持并行恢复，且体积小
    pg_dump -Fc "$DB_URL" -f "$WORK/database.dump"
    ;;
  *)
    echo "不认识的数据库类型，请按 docs/信创适配与备份容灾.md 补充对应导出命令" >&2
    exit 2
    ;;
esac

echo "[2/4] 备份附件目录：$UPLOAD_DIR"
if [ -d "$UPLOAD_DIR" ]; then
  tar -czf "$WORK/uploads.tar.gz" -C "$(dirname "$UPLOAD_DIR")" "$(basename "$UPLOAD_DIR")"
else
  echo "  附件目录不存在，跳过（若生产环境出现这行，说明配置错了）"
  : > "$WORK/uploads.absent"
fi

echo "[3/4] 记录密钥指纹（不备份密钥本身）"
if [ -n "$SECRET" ]; then
  printf '%s' "$SECRET" | sha256sum | cut -d' ' -f1 > "$WORK/secret.fingerprint"
else
  echo "  警告：MEDPLAT_SECRET 未设置，恢复后审计哈希链将无法校验" >&2
  echo "unset" > "$WORK/secret.fingerprint"
fi

echo "[4/4] 打包与清单"
# alembic 记**全部** head：本仓库是双 head（平台链 + spd 链）。
# 原先 `alembic heads | head -1` 只记一个——少了 spd 链的备份，清单看上去仍然齐全，
# 恢复演练也照样"通过"，直到有人去查 59 张 spd_ 表才发现它们从来没被核对过。
ALEMBIC_HEADS="$(cd "$(dirname "$0")/.." && alembic heads 2>/dev/null | sed -E 's/ *\(.*\)//' | tr -d '\r' | paste -sd, - || true)"
[ -n "$ALEMBIC_HEADS" ] || ALEMBIC_HEADS="unknown"
HEAD_COUNT="$(printf '%s' "$ALEMBIC_HEADS" | tr ',' '\n' | grep -c . || true)"
{
  echo "backup_at_utc=$STAMP"
  echo "db_url_scheme=${DB_URL%%:*}"
  echo "upload_dir=$UPLOAD_DIR"
  # 附件在包里的顶层目录名 = 备份时目录的 basename。恢复端据此把内容搬到
  # **当前** MEDPLAT_UPLOAD_DIR，而不是原样还原成备份时的目录名。
  echo "upload_dir_basename=$(basename "$UPLOAD_DIR")"
  echo "alembic_heads=$ALEMBIC_HEADS"
  echo "alembic_head_count=$HEAD_COUNT"
} > "$WORK/manifest.txt"

ARCHIVE="$OUT_DIR/medplat-backup-$STAMP.tar.gz"
tar -czf "$ARCHIVE" -C "$WORK" .
# 校验文件里只写 **basename**：sha256sum 记什么路径，`sha256sum -c` 就按什么路径找文件。
# 原先记的是调用时给的路径（常常是 ./backups/xxx.tar.gz 这种相对路径），
# 换一个 cwd 恢复时 `-c` 直接 "No such file or directory"，`set -e` 让 restore.sh
# 在第 0 步就退出——**备份包完好，恢复脚本却跑不起来**，而这正是最需要它的一刻。
(cd "$(dirname "$ARCHIVE")" && sha256sum "$(basename "$ARCHIVE")") > "$ARCHIVE.sha256"
echo "完成：$ARCHIVE"
echo "提醒：备份能不能用，只有恢复的时候才知道——每季度请跑一次 scripts/restore_drill.sh"
