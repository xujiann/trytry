#!/bin/sh
# 云端启动脚本：先迁移后起服务；MEDPLAT_SEED_DEMO=1 时等服务就绪后自动灌入演示数据
set -e  # 迁移失败必须中止启动（ADR-0002）——起在未迁移库上比不起更糟
PORT="${PORT:-8000}"

# ADR-0002：结构变更统一走 alembic（复数 heads——平台链 + spd 链双 head）。
# 生产环境应用内已停用 create_all，迁移失败时在启动前暴露，好过运行时缺表。
#
# A8：MEDPLAT_MIGRATE_ON_START=0 可跳过启动期迁移——多实例/滚动发布场景下，
# N 个实例同时对同一库跑 alembic 会互相踩；此时迁移由发布流程**单独执行一次**
# （见 docs/发布流程.md），实例只管起服务。默认 1，单实例行为与从前一致。
if [ "${MEDPLAT_MIGRATE_ON_START:-1}" = "0" ]; then
  echo "MEDPLAT_MIGRATE_ON_START=0：跳过启动期迁移（由发布流程单独执行 alembic upgrade heads）"
else
  alembic upgrade heads
fi

# A8：MEDPLAT_WORKERS>1 时多 worker 起 uvicorn（多核利用）。默认 1 保持现行为。
# 注意：进程内状态（如 ws 连接管理）不跨 worker 共享，多 worker 需 Redis 在位。
WORKERS="${MEDPLAT_WORKERS:-1}"
if [ "$WORKERS" -gt 1 ] 2>/dev/null; then
  uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --workers "$WORKERS" &
else
  uvicorn app.main:app --host 0.0.0.0 --port "$PORT" &
fi
UV_PID=$!

if [ "$MEDPLAT_SEED_DEMO" = "1" ]; then
  i=0
  while [ $i -lt 30 ]; do
    sleep 1
    if python -c "import httpx;httpx.get('http://127.0.0.1:$PORT/api/health',timeout=2)" 2>/dev/null; then
      python scripts/seed_demo.py "http://127.0.0.1:$PORT" || true
      break
    fi
    i=$((i+1))
  done
fi

wait $UV_PID
