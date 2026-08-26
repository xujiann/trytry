#!/usr/bin/env bash
# Codespace 建好后装依赖。只装运行时依赖，不装 ruff/mypy/playwright——
# 这个环境是给"打开就能看"的演示用的，开发工具让 `make install` 按需补。
set -euo pipefail

cd "$(dirname "$0")/../server"

# 与两个 Dockerfile 同一口径：优先装全钉版 lockfile，保证 Codespace 里跑的依赖版本
# 与 render.yaml 部署出去的那份一致（requirements.txt 仍是区间真源）。
pip install --no-cache-dir --disable-pip-version-check -r requirements.lock

echo "依赖就绪。演示服务由 postAttachCommand 自动启动（.devcontainer/start-demo.sh）。"
