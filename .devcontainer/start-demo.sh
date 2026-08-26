#!/usr/bin/env bash
# 启动演示站。刻意**不自造启动逻辑**——直接复用云端入口 server/start.sh，
# 它已经把顺序定死了：alembic upgrade heads（复数 head：平台链 + spd 链）
# → 起 uvicorn → 等 /api/health 就绪后灌演示种子。
#
# 演示环境自己抄一份启动步骤，就是下一次漂移的起点（本仓库已经吃过一次：
# 演示种子登录用的口令和部署时设的口令两处各写各的，公网演示站因此一条数据都没有）。
set -euo pipefail

cd "$(dirname "$0")/../server"

echo "启动 medplat 演示站：迁移 → 起服务 → 灌演示数据（首次约需 1 分钟）"
echo "就绪后在 PORTS 面板打开 8000 端口；要分享给别人看，先把该端口可见性改为 Public。"

exec sh start.sh
