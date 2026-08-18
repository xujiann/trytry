# CLAUDE.md

给 AI 编码代理（及新加入的人）的项目约定。**先读本文件，再动代码。**
本文件与 `docs/CURRENT_ARCHITECTURE.md`、`docs/MODULE_MAP.md`、`docs/DATA_MODEL.md`、
`docs/API_MAP.md`、`docs/DEPENDENCY_MAP.md`、`docs/TECH_DEBT.md` 六张架构地图配套使用。

---

## 0. 项目速览

- **单进程 FastAPI 单体**：县域医共体信息化平台（medplat）+ 全域慢专病全流程管理子系统（`app/spd`）。
- 规模：246 张表 / 881 个 HTTP 端点 / 89 个路由文件 / 52 个迁移；后端 Python，前端为**免构建**原生 JS SPA。
- 入口：`server/app/main.py`（`app.main:app`）。配置：`server/app/config.py`（`MEDPLAT_*` 环境变量）。
- 开发库 SQLite，生产库 PostgreSQL 16，Redis 可选。

```bash
cd server
pip install -r requirements.txt
uvicorn app.main:app --reload      # http://127.0.0.1:8000/  接口文档 /docs
python -m pytest tests/ -q         # 全量测试（e2e 默认跳过）
```

---

## 1. 通用原则（硬性规则）

1. **保持既有行为**，除非任务明确要求改变它。
2. **未经批准不重写可运行的模块。**
3. **改一个模块前，先弄清它的依赖**（查 `docs/DEPENDENCY_MAP.md`，或 grep 其 import 与被 import）。
4. **优先增量重构，不做大重写。**
5. **不要重复实现已有功能。** 本仓库已有严重的三套并行子域问题（见 §5），别再加第四套。
6. **建新工具/组件/服务/类型/接口前，先搜仓库**是否已有。横切能力多半已抽好（`deps` / `visibility` / `concurrency` / `paginate` / `events` / `formula`），只是没铺满——优先复用而非新建。
7. **公共接口保持向后兼容**，除非明确批准破坏性变更。
8. **数据库结构变更必须写迁移**（见 §4）。
9. **新功能必须带测试**（见 §6）。
10. **改 Bug 尽量带回归测试**。
11. **不得为了让 CI 变绿而删测试。**
12. **不得无理由引入新依赖。** 运行时依赖只有 13 项（`server/requirements.txt`），全无 lockfile；新增需在 PR 里说明必要性。
13. **架构级变更需要 ADR**（见 §9）。
14. **完成前必须跑测试、lint、类型检查**（见 §7）。当前仓库尚无 lint/类型配置——若你的改动引入了工具，请一并接入 CI，别只在本地跑。
15. **改动范围限定在任务本身**，不顺手重构无关代码。

---

## 2. 分支与提交纪律

- 在**指定的功能分支**上开发，不要直推默认分支或别人的分支。
- 慢专病子系统的迁移**自成一条 alembic 分支**（`branch_labels=("spd",)`）——平台迁移不得挂到 spd 链上，反之亦然。`tests/test_spd_boundary.py` 会检查这一点。
- 提交信息用中文、说明「为什么」而非只说「改了什么」，与现有历史风格一致。
- **若指定分支对应的 PR 已合并**：视为全新改动，从最新默认分支重开同名分支，不要在已合并历史上叠加。

---

## 3. 目录与分层约定

```
server/app/
  main.py         入口 + lifespan 种子化 + 三层中间件
  config.py       配置（唯一真源；MEDPLAT_REDIS_URL 例外，直接读 os.environ）
  models.py       全部平台 ORM（勿再无节制增长；见 §4）
  spd/            慢专病子系统（独立包、单向依赖、可装卸）
  routers/        接口层——目前业务逻辑内联在此（无正式 service 层）
  deps/visibility/concurrency/events/clock/…  横切基础设施
```

- **无独立 domain/service 层**：平台侧业务逻辑内联在路由里。若你要抽服务层，放 `app/services/`，别再往路由里堆。
- **子系统边界（重要）**：`app/spd` 只能经 `app/spd/platform.py` 访问平台的**模型与路由**；主 app 只能通过 `main.py` + `models.py` 触达 spd。这条单向依赖由 `tests/test_spd_boundary.py` 的 AST 扫描强制，**不要绕过它**。
- 种子常量目前寄生在路由文件里被 `main.py` 反向 import——沿用现状即可，别新增这种寄生。

---

## 4. 数据库与迁移约定

- **结构变更必须写 alembic 迁移**，且 `Base.metadata.create_all`（`main.py:113`）**不能代替迁移**——它只在开发 SQLite 上"看起来正常"，生产 PG 走迁移，漏写迁移会上线才炸（历史已发生过）。
- **迁移升级用 `alembic upgrade heads`（复数）**——本仓库有两个 head（平台链 + spd 链）。单数 `head` 会报错并漏掉 spd 的 59 张表。
- 每个迁移**必须实现 `downgrade()`**（当前 52/52 全部实现，保持这个纪录）。
- 类型约定（照抄现状，别自创）：
  - **金额**：一律用 `Money`（`= Numeric(14,2, asdecimal=False)`，`models.py:44`）。**禁止用 Float 存金额。**
  - **日期**：`String(10)`（配 `datetypes.DateStr`/`OptionalDateStr` 做入参校验）。**时间戳**：`DateTime` + `utcnow()`（naive UTC）。
  - **状态**：裸字符串，不用 Enum；取值范围写在列注释与路由 `pattern` 里。
  - **长文本**：`String(N)`（无 Text 类型），注意 1024 上限。
- **表命名**：与所在业务域一致；spd 表一律 `spd_` 前缀。
- **PII**：`id_card`/`phone` 目前明文存储、仅出口脱敏（`privacy.py`）——别在日志/响应里绕过脱敏。
- 种子数据一律**幂等"只增不改"**（查已有 code 再 `add`），不要写会覆盖现场配置的种子。

---

## 5. 已知重复子域（改之前务必知道）

同一业务概念存在**三套并行实现**，这是历史债，不是可复用的模板：

| 概念 | 慢病 | 专病 | 慢专病 |
|---|---|---|---|
| 病种目录 | `chronic_disease_types` | `disease_programs` | `spd_programs` |
| 随访 | `followups` | — | `spd_followup_*` |
| 转诊 | `referrals` | — | `spd_referral_*` |

- 加功能前先确认落在哪一套，**不要再新建第四套**，也不要把逻辑复制到另一套。
- 规则求值已有 6 处实现（`formula` / `app/rules` / `spd/rules` / quality / dataquality / prescriptions）——新增规则判断优先复用，别再造第 7 套。
- 详见 `docs/TECH_DEBT.md` 与 `docs/DATA_MODEL.md`。

---

## 6. 测试约定

- 新功能**必须带测试**；改 Bug **尽量带回归测试**（本仓库已有大量 AST 静态防复发用例，是好范式）。
- 测试跑在文件型 SQLite（`tests/conftest.py`）。涉及**并发/事务/PG 方言**的改动，请同时补 `tests/test_postgres_real.py` 一类真 PG 用例——注意它默认 skip，CI 目前也不跑，**别把"SQLite 绿了"当成"PG 也对"**。
- 写**唯一约束表**的接口**必须处理 `IntegrityError` 冲突**——有 AST 用例（`test_stage14_concurrency.py`）盯着，包括 `app/spd/routers/`。
- **不得删测试来凑 CI**。命名：新测试尽量按业务模块命名，别再沿用 `test_stageN_*` / `test_final_gapN` 这类按轮次命名的旧习惯。

---

## 7. 完成前的自检清单

声明"完成"前，至少跑：

```bash
cd server
python -m pytest tests/ -q                      # 必跑
# 若引入了 lint/类型工具，一并跑并接入 CI（当前仓库尚无，别只在本地验证）
```

- 若改了迁移：本地 `alembic upgrade heads` 能从空库跑通。
- 若改了 spd：`pytest tests/test_spd_boundary.py -q` 必须绿（边界未被破坏）。
- CI 现状：只跑 pytest，覆盖率门禁是 warning 模式（`|| true`），**不要依赖 CI 拦回归**——自己先验证。

---

## 8. 安全红线

- **不要**在代码/配置/提交里放真实密钥、口令、令牌。默认值（`admin123` / `dev-secret-*`）仅供本地。
- 处理患者/机构数据的读写，接入 `visibility` 的可见性校验与 `AccessLog` 留痕；**别新增"按 id 直取、不校验归属、无留痕"的接口**。
- 前端渲染用户数据一律先 `esc()`；`innerHTML` 插值不得漏转义。
- 涉及认证、越权、加密、审计链的改动，走 §9 的 ADR 并请人复核。

---

## 9. ADR（架构决策记录）

- **架构级变更需要 ADR**：新增/移除子系统、改依赖方向、换认证或迁移机制、动数据模型顶层决策（金额/日期/枚举策略）、破坏公共接口兼容性等。
- ADR 放 `docs/adr/`（首次创建该目录即可），一份一文件，写清：背景、决策、被否决的方案、影响。
- 现有的顶层决策已散落在各模块 docstring 与 `docs/` 文档里（`events.py` / `clock.py` / `gmcrypto.py` / `spd/__init__.py` 等的注释质量很高，可作参考与来源）。

---

## 10. 现状速查（更多细节见六张地图）

- 架构与运行拓扑 → `docs/CURRENT_ARCHITECTURE.md`
- 模块清单与职责 → `docs/MODULE_MAP.md`
- 表结构/迁移/类型 → `docs/DATA_MODEL.md`
- 接口面与鉴权 → `docs/API_MAP.md`
- 依赖与循环 → `docs/DEPENDENCY_MAP.md`
- 分级技术债 + 不可丢的优点 → `docs/TECH_DEBT.md`
- 完整 AS-IS 审计 → `docs/架构审计报告_AS-IS.md`
