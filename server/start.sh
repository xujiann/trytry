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
#
# 单 worker 分支也显式写 `--workers 1`（P2-1217）：不写时 uvicorn 的 --workers 缺省取环境变量 $WEB_CONCURRENCY，
# 托管平台常注入它，结果悄悄起了多个 worker；config.py 的多实例拒启守卫只认 MEDPLAT_WORKERS / MEDPLAT_MIGRATE_ON_START，
# 认不出这种多 worker——没配 Redis 时登出黑名单、登录锁定、限流各 worker 一份，登出过的令牌在别的 worker 上照样能用。
# worker 数只认 MEDPLAT_WORKERS 这一个开关。
#
# --no-access-log（P2-1142）：uvicorn 自带的访问日志把 path 连查询串整行写 stdout，由 docker/journald 留存——
# 按身份证号 / 手机号检索患者走的是 `?keyword=`，证件号就这样明文进了日志，PII 加密开关管不到。平台自己的访问日志
# medplat.access 只记 path，方法、状态、耗时、request_id 都在，关掉 uvicorn 那份不丢排障信息；来源 IP 看反向代理
# 日志与登录留痕（login_logs）。
WORKERS="${MEDPLAT_WORKERS:-1}"
if [ "$WORKERS" -gt 1 ] 2>/dev/null; then
  set -- uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --workers "$WORKERS" --no-access-log
else
  set -- uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --workers 1 --no-access-log
fi

# 优雅关闭（上线前审计）。容器里 PID 1 就是本脚本，`docker stop` 的 SIGTERM
# 打在它身上——原先它把 uvicorn 放后台再 `wait`，既没 `exec` 也没 `trap`：
# shell 收到 TERM 直接退出，PID 1 一退，内核把命名空间里剩下的进程**全部
# SIGKILL**。后果是每次发布/重启，**在途请求全被硬切**，`lifespan` 的收尾
# （main.py 里取消调度任务那段）永不执行。
#
# 分两条路，而不是给两条路都套一个 trap：
#
# - **不灌演示数据（也就是生产形态）→ `exec`**。让 uvicorn 直接**变成** PID 1，
#   信号由它亲自收，中间没有任何一层需要转发。这比 trap 转发更可靠——
#   trap 只在 shell 处于可中断状态时才跑得到。
# - **要灌演示数据（仅演示站）→ 后台 + trap 转发**。这条路必须在服务起来之后
#   再干一件事，没法 exec，所以老老实实转发信号并等它退干净。
if [ "$MEDPLAT_SEED_DEMO" != "1" ]; then
  exec "$@"
fi

"$@" &
UV_PID=$!
# 收到信号先转发给 uvicorn，然后摘掉 trap 再 wait 一次——第一次 wait 会被
# 信号打断并返回 128+signo，那时子进程还在收尾，直接退出等于没等。
trap 'kill -TERM "$UV_PID" 2>/dev/null' TERM INT

i=0
while [ $i -lt 30 ]; do
  sleep 1
  if python -c "import httpx;httpx.get('http://127.0.0.1:$PORT/api/health',timeout=2)" 2>/dev/null; then
    # 灌数失败不该拖垮服务（末尾的 `|| true`），但也不能再悄悄吞掉（P2-1278）：原先 admin 口令对不上时脚本 401 后
    # 抛 KeyError，被这里吞得一行不剩，演示站空着没人知道为什么。`$?` 是灌数脚本的退出码
    python scripts/seed_demo.py "http://127.0.0.1:$PORT" \
      || echo "演示数据灌入失败：scripts/seed_demo.py 退出码 $?，原因见上方输出；服务照常运行，演示数据缺失或不全" >&2 \
      || true
    break
  fi
  i=$((i+1))
done

# 第一次 wait 的退出码要用 `||` 接住（P2-1281）：本脚本开着 `set -e`，wait 被信号打断返回 128+signo 时整个脚本
# 就地退出——正是上面那句「直接退出等于没等」，PID 1 一退，收尾到一半的 uvicorn 被内核 SIGKILL。被打断时子进程还在
# 收尾（`kill -0` 探得到），摘掉 trap 再等它一次；脚本以 uvicorn 自己的退出码退出，它自己崩了的照旧原样带出去
rc=0
wait "$UV_PID" || rc=$?
if kill -0 "$UV_PID" 2>/dev/null; then
  trap - TERM INT
  rc=0
  wait "$UV_PID" || rc=$?
fi
exit "$rc"
