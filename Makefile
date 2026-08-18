# 县域医共体平台——统一命令入口。
# 六项能力：build / lint / typecheck / test-unit / test-integration / test-smoke。
# 所有命令在 server/ 下执行；不改变运行时行为。

SERVER := server
PY := python

.DEFAULT_GOAL := help

.PHONY: help install build lint typecheck test-unit test-integration test-smoke test verify docker-build

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
typecheck:  ## 渐进式类型检查（mypy，仅查已注解代码）
	cd $(SERVER) && mypy app

# ---- 三档测试 ----
test-unit:  ## 单元/接口测试：进程内 SQLite 快速套件（无外部依赖）
	cd $(SERVER) && $(PY) -m pytest tests/ -q -m "not integration and not smoke and not e2e"

test-integration:  ## 集成测试：真 PostgreSQL（需 MEDPLAT_PG_TEST_URL）
	cd $(SERVER) && $(PY) -m pytest tests/ -q -m integration

test-smoke:  ## 冒烟测试：应用可启动 + 核心接口有响应 + 产出指标
	cd $(SERVER) && $(PY) -m pytest tests/ -q -m smoke

# ---- 聚合 ----
test: test-unit test-smoke  ## 无外部依赖的可跑测试（unit + smoke）

verify:  ## 提交前自检（对应 CLAUDE.md 第14条）
	$(MAKE) build
	$(MAKE) lint
	-$(MAKE) typecheck   # 渐进式基线（mypy 存量 41，见 ROADMAP）：warning 模式，不阻断 verify
	$(MAKE) test-unit
