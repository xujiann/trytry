#!/bin/sh
# 云端启动脚本：起服务；MEDPLAT_SEED_DEMO=1 时等服务就绪后自动灌入演示数据
PORT="${PORT:-8000}"
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
