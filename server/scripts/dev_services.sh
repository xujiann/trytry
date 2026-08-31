#!/bin/sh
# 本机拉起「只给测试用」的 PostgreSQL + Redis，让 `make test-integration` 真的跑得起来。
#
# 为什么要这个脚本：integration 档（真 PG 的并发/方言用例、迁移-模型结构漂移 PG 档、
# 调度锁的真 Redis 档）在没有这两个服务时会**整档 skip 而退出码为 0**——"没跑"与
# "全对"的绿灯长得一模一样（CI 因此专门加了闸门数执行条数）。开发机上少了一条随手
# 起服务的路，这些档就只能赊给 CI，而 CLAUDE.md §6 的原则恰恰是"别把 SQLite 绿了
# 当成 PG 也对"。实测：Debian/Ubuntu 系的开发容器里 postgresql-16 与 redis-server
# 往往已经装好，只是没跑起来。
#
# 用法：
#   eval "$(server/scripts/dev_services.sh start)"   # 起服务并导出两个 *_TEST_URL
#   make test-integration
#   server/scripts/dev_services.sh stop
#
# **仅供本机开发/测试**：绑 127.0.0.1、走 trust 认证、数据目录在 /tmp（重启即弃）。
# 它不是部署路径，也不产生任何需要保管的凭据——生产部署见 docs/运维手册.md。
set -eu

PGDATA=${MEDPLAT_DEV_PGDATA:-/tmp/medplat-pgdata}
PGPORT=${MEDPLAT_DEV_PGPORT:-55432}
PGDB=${MEDPLAT_DEV_PGDB:-medplat_test}
REDIS_PORT=${MEDPLAT_DEV_REDIS_PORT:-56379}
PG_URL="postgresql+psycopg2://postgres@127.0.0.1:${PGPORT}/${PGDB}"
REDIS_URL="redis://127.0.0.1:${REDIS_PORT}/0"

# PG 的服务端命令在 Ubuntu 上不在 PATH 里（/usr/lib/postgresql/<版本>/bin），取最高版本。
pg_bin() {
  for dir in $(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V -r); do
    if [ -x "$dir/pg_ctl" ]; then echo "$dir"; return 0; fi
  done
  command -v pg_ctl >/dev/null 2>&1 && dirname "$(command -v pg_ctl)" && return 0
  return 1
}

# PG 拒绝以 root 身份运行，root 下一律换到 postgres 账户执行。
as_pg_user() {
  if [ "$(id -u)" = "0" ]; then
    id postgres >/dev/null 2>&1 || {
      echo "以 root 运行但没有 postgres 账户：请改用普通用户，或先 useradd -m postgres" >&2
      exit 1
    }
    su postgres -c "$1"
  else
    sh -c "$1"
  fi
}

start_pg() {
  BIN=$(pg_bin) || { echo "找不到 PostgreSQL 服务端（apt-get install postgresql）" >&2; exit 1; }
  if [ ! -s "$PGDATA/PG_VERSION" ]; then
    mkdir -p "$PGDATA"
    [ "$(id -u)" = "0" ] && chown -R postgres "$PGDATA"
    as_pg_user "$BIN/initdb -D $PGDATA -U postgres --auth-local=trust --auth-host=trust" >&2
  fi
  if ! as_pg_user "$BIN/pg_ctl -D $PGDATA status" >/dev/null 2>&1; then
    # unix socket 也放数据目录：Debian 默认的 /var/run/postgresql 在容器里常不可写，
    # 而我们本来就只用 TCP 连（127.0.0.1），没必要依赖系统目录。
    as_pg_user "$BIN/pg_ctl -D $PGDATA -l $PGDATA/server.log \
      -o '-p $PGPORT -c listen_addresses=127.0.0.1 -c unix_socket_directories=$PGDATA' start" >&2
  fi
  # createdb 幂等：已存在就当没事发生（脚本要能重复跑）
  as_pg_user "$BIN/psql -h 127.0.0.1 -p $PGPORT -U postgres -tAc \
    \"SELECT 1 FROM pg_database WHERE datname='$PGDB'\"" 2>/dev/null | grep -q 1 \
    || as_pg_user "$BIN/createdb -h 127.0.0.1 -p $PGPORT -U postgres $PGDB" >&2
}

start_redis() {
  command -v redis-server >/dev/null 2>&1 || {
    echo "找不到 redis-server（apt-get install redis-server），跳过 Redis" >&2; return 0
  }
  redis-cli -p "$REDIS_PORT" ping >/dev/null 2>&1 && return 0
  # --save '' 关快照：测试数据没有留存价值，也不该在 /tmp 里留 dump.rdb
  redis-server --port "$REDIS_PORT" --bind 127.0.0.1 --daemonize yes --save '' >&2
}

case "${1:-start}" in
  start)
    start_pg
    start_redis
    echo "export MEDPLAT_PG_TEST_URL='$PG_URL'"
    echo "export MEDPLAT_REDIS_TEST_URL='$REDIS_URL'"
    ;;
  stop)
    BIN=$(pg_bin) && as_pg_user "$BIN/pg_ctl -D $PGDATA stop" >&2 2>/dev/null || true
    redis-cli -p "$REDIS_PORT" shutdown nosave >/dev/null 2>&1 || true
    ;;
  env)
    echo "export MEDPLAT_PG_TEST_URL='$PG_URL'"
    echo "export MEDPLAT_REDIS_TEST_URL='$REDIS_URL'"
    ;;
  *)
    echo "用法: $0 {start|stop|env}" >&2
    exit 2
    ;;
esac
