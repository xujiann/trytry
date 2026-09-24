# 县域医共体平台——统一命令入口。
# 六项能力：build / lint / typecheck / test-unit / test-integration / test-smoke。
# 所有命令在 server/ 下执行；不改变运行时行为。

SERVER := server
PY := python

.DEFAULT_GOAL := help

.PHONY: help install build lint typecheck test-unit test-integration test-smoke test verify docker-build probe-lists

help:  ## 列出可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## 安装依赖（含开发工具）
	cd $(SERVER) && pip install -r requirements.txt -r requirements-dev.txt

# ---- build：Python 无编译产物，"build" = 依赖就绪 + 字节编译过一遍(抓语法错) + 迁移图能解析到 heads ----
build:  ## 构建校验：字节编译 app + 校验 alembic 迁移图（双 head）
	cd $(SERVER) && $(PY) -m compileall -q app
	@cd $(SERVER) && command -v alembic >/dev/null 2>&1 \
		&& { alembic heads | grep -q . && echo "alembic heads OK（升级用复数：alembic upgrade heads）"; } \
		|| echo "alembic 未安装，跳过迁移图校验（make install 后可用）"

docker-build:  ## 构建生产镜像
	docker build -t medplat:local -f $(SERVER)/Dockerfile $(SERVER)

# ---- lint ----
lint:  ## 代码风格与未用 import 检查（ruff）
	cd $(SERVER) && ruff check .

lint-fix:  ## 自动修可修的 lint 问题（谨慎，会改代码）
	cd $(SERVER) && ruff check --fix .

# ---- typecheck ----
typecheck:  ## 类型检查（mypy，仅查已注解代码；存量已清零，新增报错即阻断）
	# 先验环境：mypy 与依赖不在同一环境时，ignore_missing_imports 会把
	# 第三方库静默当成 Any，报出的错误数远小于真实值（详见脚本头部注释）。
	cd $(SERVER) && $(PY) scripts/check_mypy_env.py && mypy app

# ---- 三档测试 ----
test-unit:  ## 单元/接口测试：进程内 SQLite 快速套件（无外部依赖）
	cd $(SERVER) && $(PY) -m pytest tests/ -q -m "not integration and not smoke and not e2e"

test-integration:  ## 集成测试：真 PostgreSQL（需 MEDPLAT_PG_TEST_URL）
	cd $(SERVER) && $(PY) -m pytest tests/ -q -m integration

test-smoke:  ## 冒烟测试：应用可启动 + 核心接口有响应 + 产出指标
	cd $(SERVER) && $(PY) -m pytest tests/ -q -m smoke

# 端到端档：`make verify` **不跑它**（-m "not e2e"），CI 有独立 job。
# 改了界面的**交互形态**（prompt/confirm ↔ 页内模态框、按钮换容器）必须在本地跑一遍——
# 这类改动单元档一条都不会红，红的是 e2e，而那要等推上去半小时后才知道
# （2026-09-16 实测：出报告改模态框，CI run 581 红在"点不到左侧导航"）。
# 容器镜像预装了 chromium 但版本目录与 pip 装的 playwright 对不上，
# 所以指一下内核路径，不必再下一份（找不到时留空，由 playwright 自己找）。
PW_CHROMIUM ?= $(firstword $(wildcard /opt/pw-browsers/chromium-*/chrome-linux/chrome))

test-e2e:  ## 端到端测试：真拉起 uvicorn + Playwright 驱动三端（需 playwright 与 chromium）
	cd $(SERVER) && PLAYWRIGHT_CHROMIUM_PATH=$(PW_CHROMIUM) $(PY) -m pytest tests/e2e -q --e2e

probe-lists:  ## 清单越权探针：新机构调全部清单接口，不得看到别家患者（未登记的暴露退出码 1，P1-73）
	cd $(SERVER) && $(PY) scripts/probe_list_exposure.py

# ---- 聚合 ----
test: test-unit test-smoke  ## 无外部依赖的可跑测试（unit + smoke）

verify:  ## 提交前自检（对应 CLAUDE.md 第14条）
	$(MAKE) build
	$(MAKE) lint
	$(MAKE) typecheck
	$(MAKE) test-unit
