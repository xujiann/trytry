# ADR-0002：生产迁移统一走 alembic，停用 create_all 建表

- 状态：Accepted
- 日期：2026-08-18（提案）/ 2026-08-20（批准：平台负责人）
- 相关：`server/app/main.py:113`、`docs/数据模型治理.md`、`docs/运维手册.md`、`tests/test_schema_governance.py`

## Problem

应用启动时 `main.py:113` 无条件执行 `Base.metadata.create_all(bind=engine)`，与 alembic 迁移**双轨并存**。`create_all` 只建"不存在的表"、不改列：模型加了列而漏写迁移时，开发 SQLite 会"看起来正常"，生产 PostgreSQL 因表已存在什么也不做 → **上线才炸**。这个坑已真实发生过（`alembic/versions/a7b8c9d0e1f2_补迁移_ESB_病历质控_支付对账九表.py:4` 的注释即记录）。同时仓库三个部署产物（`start.sh`/`Dockerfile`/`docker-compose.yml`）**无一执行迁移**，README 还把 `docker compose up` 与 `alembic upgrade head`（单数，双 head 下会报错）并列。约束：开发体验依赖 create_all 的"零配置起库"，不能简单粗暴删掉。

## Options

- **方案 A：维持现状**（双轨，靠人记得先 `alembic upgrade heads`）。
- **方案 B：生产禁用 create_all**——`main.py` 里按 `settings.is_production` 跳过 `create_all`；生产改由启动前显式 `alembic upgrade heads`（写进部署产物）。开发仍走 create_all。
- **方案 C：彻底删除 create_all**——所有环境（含测试）统一走 alembic。
- **方案 D：保留 create_all + 加漂移守卫**（已部分做到：`test_模型表零漂移`），双轨但用测试兜住。

## Advantages

| 方案 | 优点 |
|---|---|
| A | 零改动 |
| B | 生产走单一可控路径；开发/测试仍零配置起库；改动面小 |
| C | 语义最干净，无双轨 |
| D | 不改启动逻辑，仅加保险丝 |

## Disadvantages

| 方案 | 缺点 |
|---|---|
| A | 漏迁移上线才发现的风险持续存在 |
| B | 需在 `main.py` 加环境分支；部署产物要补迁移步骤；测试仍用 create_all（与生产路径不同） |
| C | 测试建库要跑 52 个迁移，慢且脆；开发起库要先迁移，体验退化 |
| D | 双轨仍在；守卫只查"表缺失"，查不出"列缺失/类型漂移" |

## Migration cost

方案 B（推荐）：小到中。
- 代码：`main.py` 加 `if not settings.is_production: Base.metadata.create_all(...)`（一处，属"改代码"，本 ADR 只提案不实施）。
- 部署：`docker-compose.yml` 加一个 `migrate` 初始化步骤 / `start.sh` 起服务前 `alembic upgrade heads`；README 修正为复数 `heads`。
- CI：已具备真 PG 迁移验证（`test_postgres_real.py` + quality/test job），把它转成阻断即可。
- 无数据迁移、无回填。

## Risk

- 方案 B 风险低：开发路径不变；生产多一步 `upgrade heads`，失败会在启动前暴露（比运行时缺表好）。
- 主要风险是"部署脚本改了但某处遗漏迁移步骤"→ 用 CI 的迁移-from-空库验证 + 启动健康检查缓解。
- 不做（方案 A）的风险已发生过一次，且随表增长上升。

## Recommendation

采纳**方案 B**（生产禁用 create_all + 部署产物补 `alembic upgrade heads` + README 改复数），并把 `test_postgres_real.py` 的迁移验证在 CI 转为阻断门。方案 C 体验代价过高，D 保护力不足。**已批准并实施**：`main.py` 生产跳过 create_all（守卫测试 `test_adr0002_create_all_guard.py`）、
`start.sh` 启动前 `alembic upgrade heads`、README 修正复数、CI 集成/迁移门与覆盖率门禁转阻断
（落地时实测覆盖率 87%）。
