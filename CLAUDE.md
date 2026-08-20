# CLAUDE.md

给 AI 编码代理（及新加入的人）的项目约定。**先读本文件，再动代码。**
本文件与 `docs/CURRENT_ARCHITECTURE.md`、`docs/MODULE_MAP.md`、`docs/DATA_MODEL.md`、
`docs/API_MAP.md`、`docs/DEPENDENCY_MAP.md`、`docs/TECH_DEBT.md` 六张架构地图配套使用。
体系总纲见 `docs/工程治理体系.md`（规则/地图/分级/棘轮/ADR/工作流一处入口）。

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

统一命令入口（仓库根 `Makefile`，`make help` 列全部）：

```bash
make install           # 装依赖（含开发工具 ruff/mypy/playwright）
make build             # 字节编译 + 校验 alembic 迁移图（双 head）
make lint              # ruff
make typecheck         # mypy（渐进式，仅查已注解代码）
make test-unit         # 进程内 SQLite 快速套件（无外部依赖）
make test-integration  # 真 PostgreSQL（需 MEDPLAT_PG_TEST_URL）
make test-smoke        # 应用可启动 + 核心接口有响应
make verify            # build + lint + typecheck + test-unit（提交前自检）
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
14. **完成前必须跑测试、lint、类型检查**（见 §7）。`make verify` 一把跑全。注意 mypy 必须与项目依赖装在**同一环境**，否则结果是假的——`make typecheck` 会先跑探针拦住这种情况。
15. **改动范围限定在任务本身**，不顺手重构无关代码。（其正向补充是"童子军法则"，见 §12：在你**已经动到**的代码附近，做小而安全的清理是鼓励的；无关的大重构不是。）

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
- **核心表已冻结**（`users`/`organizations`/`patients`/`encounters`/`admissions`）：改其列需先写 ADR，再更新 `tests/test_schema_governance.py` 的 `FROZEN_CORE_COLUMNS` 快照。**新表必须带 `created_at`**（棘轮强制）。改了模型重跑 `python scripts/dump_schema.py`。详见 `docs/数据模型治理.md`。
- **核心数据是不可变定义**：核心概念只有一个权威表（`patients`/`organizations`/`users`/`encounters`/`admissions`/`resident_accounts`），**不得另造平行主数据**；人物身份（`id_card`/`ehc_no`）只存 `patients`，别处一律外键 `patient_id`。金额用 `Money`、日期用 `DateStr`，别自造。由 `tests/test_core_data_invariants.py` 强制，详见 `docs/核心数据不可变定义.md`。

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
make verify        # = build + lint + typecheck + test-unit（对应第14条）
make test-smoke    # 若动了启动/核心链路
make test-integration   # 若动了迁移/PG 方言相关（需 MEDPLAT_PG_TEST_URL）
```

- 若改了迁移：`make build` 校验迁移图，且本地 `alembic upgrade heads` 能从空库跑通。
- 若改了 spd：`pytest tests/test_spd_boundary.py -q` 必须绿（边界未被破坏）。
- **lint 存量已清零并转为阻断**（`ruff` 起步规则集 0 项）：新增 lint 报错会拦下 CI。
- **typecheck 存量已清零并转为阻断**（`mypy` 只查已注解代码，0 处）：新增类型报错会拦下 CI。范围仍是渐进式的（不开 `--check-untyped-defs`），扩大范围属单独任务。
- ⚠️ **跑 mypy 前先确认环境**：`pyproject.toml` 开了 `ignore_missing_imports=true`，所以当 mypy 解析不到某个库时它**不报错、而是把整个库当成 `Any`**——依赖它的代码全部「通过」。隔离安装（`uv tool install mypy` / `pipx`）尤其容易踩：那个环境里没有 SQLAlchemy，同一份代码本地报 41 处、CI 报 187 处。`make typecheck` 会先跑 `scripts/check_mypy_env.py` 探针拦住这种假绿；**别拿隔离环境里的数字下结论**。
- CI 现状：`test` job 跑 unit+smoke + integration（真 PG）+ 覆盖率门禁，**均为阻断**；`quality` job 跑 build + lint（阻断）+ mypy 环境探针（阻断）+ typecheck（阻断）。**六项全阻断**。依旧**先自己 `make verify`**，别把 CI 当第一道防线。
- **Python 版本只有一处真源**：CI 的 `PYTHON_VERSION`、两个 Dockerfile 的 `FROM python:`、`pyproject.toml` 的 ruff `target-version` 与 mypy `python_version` 必须同版（现为 **3.12**），由 `tests/test_python_version_alignment.py` 钉住。升级要四处一起改，且**先在目标版本上跑通 `make verify`** 再改。

---

## 8. 安全红线

- **不要**在代码/配置/提交里放真实密钥、口令、令牌。默认值（`admin123` / `dev-secret-*`）仅供本地。
- 处理患者/机构数据的读写，接入 `visibility` 的可见性校验与 `AccessLog` 留痕；**别新增"按 id 直取、不校验归属、无留痕"的接口**。
- 前端渲染用户数据一律先 `esc()`；`innerHTML` 插值不得漏转义。
- 涉及认证、越权、加密、审计链的改动，走 §9 的 ADR 并请人复核。

---

## 9. ADR（架构决策记录）

- **架构级变更需要 ADR**：新增/移除子系统、改依赖方向、换认证或迁移机制、动数据模型顶层决策（金额/日期/枚举策略）、破坏公共接口兼容性等。
- ADR 放 `docs/adr/`（已建立，含索引 `README.md` 与模板 `0000-template.md`），一份一文件。**固定七段**：problem / options / advantages / disadvantages / migration cost / risk / recommendation。
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
- 模块分级（KEEP/IMPROVE/REFACTOR/REPLACE）→ `docs/模块分级_KEEP_IMPROVE_REFACTOR_REPLACE.md`
- 接口标准与治理（混乱代码→标准接口，棘轮只进不退）→ `docs/接口标准与治理.md`
- 数据模型治理（输出Schema→冻结核心表→逐步迁移）→ `docs/数据模型治理.md`；权威列表 `docs/schema/SCHEMA.md`（`scripts/dump_schema.py` 生成）
- 核心数据不可变定义（一个概念一个权威表，不得另造）→ `docs/核心数据不可变定义.md`
- 架构决策记录（ADR，七段式：problem/options/…/recommendation）→ `docs/adr/`（索引见 `docs/adr/README.md`）
- 特征化测试指引（重构前的安全网）→ `docs/特征化测试指引.md`
- 日常开发工作流（每日循环：晨间→定向阅读→PLAN→审批→实现→测试→/review→PR→merge）→ `docs/日常开发工作流.md`；今日待办 → `ROADMAP.md`

---

## 11. 接口标准（新增/改动端点必读）

- **每个端点声明 `response_model`**；列表走 `deps.paginate`；错误用 `HTTPException(detail=...)`。
- **治理不得改响应字节**（第7条向后兼容）：`response_model` 字段须与当前输出一一对应，用特征化测试守住。
- 棘轮 `tests/test_api_contract_governance.py` 会拦住"新端点漏契约"——欠账只许变小。逐块配方见 `docs/接口标准与治理.md`。

---

## 12. 童子军法则（Boy Scout Rule）

> **离开代码时，比你来的时候干净一点。**

这是第 15 条"限定范围"的**正向补充**：不为清理而清理、不借机大重构，但在**你已经动到的
那段代码**上，顺手把小问题修掉。改一个模块时——

1. **不做无关的大重构**（与第 2、15 条一致：REFACTOR/REPLACE 级别的改动走对应任务与 ADR，不夹带）。
2. **顺手修就近的小问题**（仅在安全时）：错位的 docstring、未用 import、拼写、明显笔误、
   一行就能修且带回归证据的小 bug（如 `monitor.py` 的 `"success"`/`"succeeded"`）。
3. **就近改进命名**：让新写/改写的名字读起来像周围的代码；**不**全局改名既有公共符号（那是破坏性变更）。
4. **补上缺失的测试**：你动到的分支若没测试，补一条；动遗留逻辑前先补特征化网（见 `docs/特征化测试指引.md`）。
5. **就近去重**（仅当直接相关）：你正在改的这段刚好重复了已有工具，就复用它（`deps`/`visibility`/`paginate`/`concurrency`/`events`）；**不**顺手去合并三套并行子域那种大重复（那是 ADR-0003）。
6. **把碰到的代码往目标架构挪一小步**：目标见 `docs/模块分级_*.md` 与治理文档——例如给你改的端点补 `response_model`、给你新增的表带 `created_at`、把内联业务逻辑抽一点到 `app/services/`。一次挪一小步，不求到位。

### 边界（别把好意变成风险）

- **每次清理都要 byte 安全**：碰到的是行为敏感代码，先有测试/特征化网兜住，再动；改公共接口响应体属破坏性变更，走第 7 条。
- **清理与功能改动尽量分开提交**，让 review 能分辨"这是修 bug"还是"这是顺手擦干净"。
- **修不动的先登记，别硬修**：清理超出"小而安全"就停手，写进 `docs/TECH_DEBT.md` 或开任务/ADR，而不是把大改夹带进无关提交。
- 童子军法则是**只进不退**的日常版：每次触碰都让欠账（lint/类型/契约/created_at 棘轮）少一点，绝不让它变多。
