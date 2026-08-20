#!/bin/sh
# 云端启动脚本：先迁移后起服务；MEDPLAT_SEED_DEMO=1 时等服务就绪后自动灌入演示数据
set -e  # 迁移失败必须中止启动（ADR-0002）——起在未迁移库上比不起更糟
PORT="${PORT:-8000}"

# ADR-0002：结构变更统一走 alembic（复数 heads——平台链 + spd 链双 head）。
# 生产环境应用内已停用 create_all，迁移失败时在启动前暴露，好过运行时缺表。
alembic upgrade heads

uvicorn app.main:app --host 0.0.0.0 --port "$PORT" &
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
