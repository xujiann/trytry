# 模块分级：KEEP / IMPROVE / REFACTOR / REPLACE

> 对平台每个模块给出处置分级，作为演进节奏的依据。结论来自六路审计
> （见 `docs/架构审计报告_AS-IS.md` 与六张地图），仅为**判断**，本文件不改动任何代码。

## 分级标准

| 类 | 含义 | 判据 | 原则 |
|---|---|---|---|
| **A** | KEEP | 功能正确 · 结构合理 · 测试较好 | **不动** |
| **B** | KEEP + IMPROVE | 功能正常 · 代码一般 · 可逐步优化 | 逐步优化，不重写 |
| **C** | REFACTOR | 严重耦合 · 重复代码 · 难以扩展 · 超大 | 逐块重构 |
| **D** | REPLACE | 设计根本错误 / 安全风险无法修补 / 无法测试 / 技术路线废弃 / 维护成本高于重做 | 才真正重写 |

> 🔴 = 附带 P0 级安全/正确性缺陷，**优先级独立于分级**：即便某模块整体只是 B，
> 🔴 标记的洞也要尽快堵，不必等重构。安全洞的修法通常是"加校验/加留痕"这类**局部**改动，
> 不构成"重写"，因此不把它单独拔高到 D。

## 总览（按模块数）

| 类 | 数量 | 占比 | 一句话 |
|---|---:|---:|---|
| A KEEP | 14 | ~11% | 横切基础设施里的精华，别碰 |
| B KEEP+IMPROVE | 63 | ~50% | 绝大多数业务路由——能用、有测试、但内联逻辑/无契约/小瑕疵 |
| C REFACTOR | 49 | ~39% | 倾倒场、统计簇、三套并行子域、超大文件、前端 |
| **D REPLACE** | **0** | **0%** | **没有模块达到"必须重写"的门槛**（见文末论证） |

---

## A 类：KEEP（不动）

横切基础设施里"功能正确 + 结构合理 + 测试较好"的部分。这些是本仓库的**优点资产**，
重构其它模块时应当**复用**它们而不是碰它们。

| 模块 | 依据 |
|---|---|
| `app/concurrency.py` | 7 个原子写原语，模块文档记录 3 次真实事故，配 AST 防复发扫描（`test_stage14_concurrency.py`）。质量最高的一块。 |
| `app/clock.py` | naive UTC 统一时间源，记录了 aware/naive 混用事故；小而聚焦。 |
| `app/events.py` | 同事务同步事件总线，白名单防拼错，边界测试覆盖。 |
| `app/datetypes.py` | 日期入参校验，配 `test_stage12_dialect.py` 方言测试。 |
| `app/database.py` | 唯一 Base + engine，37 行，无可优化面。 |
| `app/gmcrypto.py` | SM3 纯实现对齐国标测试向量；明确不实现 SM2/SM4（克制正确）。 |
| `app/privacy.py` | 脱敏，小而聚焦。 |
| `app/formula.py` | AST 白名单数值求值，供绩效/基金；干净。属"规则引擎簇"但自身无债。 |
| `app/spd/rules.py` | 纯函数规则求值；已建 37 条特征化测试（`test_spd_rules_characterization.py`）钉住行为。属收敛目标，但**动它之前已有网**。 |
| `app/sms.py` / `app/wechat.py` | 双通道（stub/真实现）结构清晰，切换只配环境变量。 |
| `app/routers/auth.py` | 登录 + 防爆破（按用户名锁 + 按 IP 限速），安全测试覆盖。 |
| `app/routers/organizations.py` | 28 行，schema 化，第一期模块，测试覆盖。 |
| `app/routers/dictionaries.py` | 四统一字典 + 批量导入，边界清楚。 |

> 种子模块 `dict_seed.py` / `chronic_seed.py` / `data/*_seed.py`：幂等"只增不改"，视为**数据**而非逻辑，保持不动（其中"病种码与 spd 撞码"的问题是 C 类跨模块债，不在种子文件本身）。

---

## B 类：KEEP + IMPROVE（逐步优化，不重写）

功能正常、有测试，但普遍存在：内联业务逻辑（无 service 层）、响应用裸 dict（无 `response_model`）、
或**局部**可修的小瑕疵/安全洞。**这是绝大多数业务路由的归宿**——它们不该被重写，只需逐步补齐。

### 横切基础设施（B）

| 模块 | 可优化点 |
|---|---|
| `app/deps.py` | 核心且有测试，但权限点只覆盖写方法、`require_admin` 不认权限点（结构缺口，非耦合）。 |
| `app/visibility.py` | 设计与文档俱佳，但仅 48/89 路由接入、读侧覆盖有盲区——**扩大采用**即可。 |
| `app/security.py` | 自制 JWT 可用有测试；与审计链**共用一把密钥**，建议分离。 |
| `app/audit_chain.py` | 哈希链正确但可被**末尾截断**不被发现——加序号绑定。 |
| `app/config.py` | 生产守卫是黑名单字面量比对（被 compose 绕过）——改熵/长度校验；`MEDPLAT_REDIS_URL` 收进 Settings。🔴 |
| `app/state_store.py` | Redis/内存双态可用；客户端每次新建、影子配置——收口即可。 |
| `app/scheduler.py` | 固定间隔调度可用；`_release_lock` 不校验持有者会误删他实例锁——**局部修**。🔴 |
| `app/jobs.py` | 定时任务清晰；`JobRun` 无界增长，补一个清理任务。 |
| `app/ws.py` | 进程内 WebSocket 可用；多实例需接 Redis Pub/Sub（已文档化）。 |
| `app/notify.py` | 干净；随业务事务提交的约定正确。 |
| `app/monitor.py` | 进程内指标；`os.uname()` import 期硬依赖（非 Linux 崩）——护一下。 |
| `app/schemas.py` | 契约只覆盖第一期 15 模块（14% 端点）——**决定去留**：收编或至少给 spd 补 `response_model`。 |

### 业务路由（B）——默认档

以下路由**能用、有测试、但内联逻辑 + 裸 dict**，逐步优化即可（补 `response_model` / 走 `paginate` / 抽 service）：

`users` · `rbac` · `patients`(🔴 keyword 检索无留痕) · `encounters` · `exams` · `pharmacy` · `prescriptions`(🔴 列表无 org 过滤) · `surgery` · `pathology` · `clinical_docs` · `outpatient_docs` · `medication` · `tcm` · `tcm_heritage` · `telemedicine` · `maternal` · `vaccination` · `vaccine_supply`(🔴 受种者名单无守卫) · `infectious` · `surveillance` · `eldercare` · `publichealth` · `emergency` · `consultations` · `contracts` · `appointments` · `credentials` · `certs` · `checkups` · `blood` · `medwaste` · `materials` · `staffing` · `workflows` · `dataquality` · `accounting` · `insurance` · `cost` · `drgs` · `resources` · `projects` · `todos` · `notifications` · `knowledge` · `education` · `org_groups` · `jobs` · `access_logs` · `integration` · `printing`(🔴 按 id 跨机构打印，无留痕) · `attachments`(🔴 按 id 越权下载，无留痕)

### SPD 子系统基础设施（B）

| 模块 | 可优化点 |
|---|---|
| `app/spd/platform.py` | 适配层抽象好；`PLATFORM_MODELS` 死导出、与边界测试白名单两份各自维护。 |
| `app/spd/service.py` | 局部 service 层，方向正确；覆盖面可再扩。 |
| `app/spd/subscribers.py` | 干净，重复注册有防护。 |
| `app/spd/collectors.py` | 注册表模式好；`register_collector` 死代码、`collect_internal` 只 count 不写库。 |
| `app/spd/callcenter.py` | 双通道；`set_call_provider` 死代码（无测试用）。 |
| `app/spd/jobs.py` | 4 任务清晰；反向 import `routers.care`（分层倒置）。 |
| `app/spd/reporting.py` | 段落渲染注册表好；`_score` 忽略 org_id（各机构收相同全域数据）🔴、反向 import `routers.assess`。 |
| `app/spd/seed.py` | 幂等；病种码与 `chronic_seed` 撞码（跨模块 C 债）。 |
| `app/routers/monitor.py` | 🔴 `status != "success"` vs 写入端 `"succeeded"`，把每次成功当失败——**一行修**。 |

---

## C 类：REFACTOR（逐块重构）

严重耦合 / 重复 / 难扩展 / 超大。这些**不重写**，而是拆分、去重、抽层——增量进行。

### 倾倒场与超大混合路由

| 模块 | 行 | 问题 | 重构方向 |
|---|---:|---|---|
| `app/routers/gapfill.py` | 1123 | 7 业务 / 6 前缀，按验收条目号分区，鉴权分裂、docstring 错位 | 按前缀拆回各业务文件 |
| `app/routers/service_extras.py` | 522 | 7 无关业务塞一文件，裸 `/api` 前缀 | 同上 |
| `app/routers/quality.py` | 911 | 不良事件 + 病历质控 + 院感三混，含 150 行内联质控引擎 | 拆三块，引擎抽独立模块（先补特征化网） |
| `app/routers/admin_mgmt.py` | 844 | 人事/财务/资产/公文/排班/质控 7 子系统 | 按子系统拆 |
| `app/routers/billing.py` | 820 | 收费 + 结算 + 支付网关，网关 Protocol/Mock 内联在路由 | 网关抽基础设施层 |
| `app/routers/esb.py` | 661 | 消息中间件（队列/编排/重试）内联在路由，跨 import patients/integration | 抽 ESB 引擎模块 |
| `app/routers/portal.py` | 1332 | 混 5 类关注点（含整套第二认证子系统）🔴 debug_code 回显/遗留免登录/代管无留痕 | 认证子系统独立；只读投影抽读模型 |

### 统计簇（重叠聚合，需合并口径）

`app/routers/analytics.py`(683) · `metrics.py`(515) · `reports.py`(231) · `performance.py`(208)
——四者都在做"跨机构统计聚合"，`org_scorecards` 被三处 import。合并到统一指标层。

### 三套并行子域（去重收敛，头号结构债）

`app/routers/chronic.py` · `disease_programs.py` · `referrals.py` · `followups.py`
——病种目录 / 入组 / 随访 / 转诊在慢病、专病、慢专病三处各一套，居民端两套 referrals 数据孤岛。
收敛口径（先给各侧补特征化网，再统一）。

### 编排耦合

`app/routers/inpatient.py`(516)——`discharge()` 一个函数内延迟 import drgs/followups/billing 三个兄弟路由。抽"出院编排" service。

### 超大 God 文件

| 模块 | 行 | 重构方向 |
|---|---:|---|
| `app/models.py` | 3950 | 187 类 + `import *` 环，按业务域拆包（文件内 `# ====` 分区已是现成边界） |
| `app/main.py` | 517 | lifespan 134 行种子化 + 审计中间件串行点🔴，抽种子模块、修审计并发 |
| `app/spd/models.py` | 1398 | 59 表，随平台 models 一起分域 |

### SPD 超大路由

| 模块 | 行 | 问题 |
|---|---:|---|
| `app/spd/routers/config.py` | 1547 | 16 类实体 CRUD 平铺，PATCH/DELETE 整片未测 → 先补测再拆 |
| `app/spd/routers/population.py` | 1397 | 6 业务流，单端点 48 行 |
| `app/spd/routers/care.py` | 1186 | 8 独立领域 |
| `app/spd/routers/followup.py` | 1122 | 全院通用能力被困在可卸载子系统 |
| `app/spd/routers/assess.py` | 1099 | 指标/考核/积分三合一 |
| `app/spd/routers/workbench.py` | 956 | 120 行/端点，`region_stats` 假 `period` 参数 |
| `app/spd/routers/portal.py` | 956 | 与平台居民端重复的档案投影 |
| `app/spd/routers/tasks.py` | 808 | 路径 + 统一任务 |
| `app/spd/routers/referral.py` | 632 | 🔴 转诊审核无机构层级校验（单 doctor 可伪造转诊链）、station 层级值域不一致 |

### 前端（免构建 SPA，8 个模块）

`static/core.js`(1089，公共层却含 15 页面函数 + 反向依赖) · `pages-clinical.js`(1840) · `pages-mgmt.js`(1801，🔴 会计科目 XSS) · `pages-spd.js`(1289) · `pages-public.js`(926) · `m/m.js`(1113，正则判登录失效) · `m/doctor.js`(701)
——89 个 render 手抄同一模板、无组件抽象、三套前端各写一遍 `$`/`esc`/`api`。**免构建是既定约束，不是重写理由**：抽 `panel()`/`crudPage()` 组件、合并三套工具函数即可。

---

## D 类：REPLACE（真正重写）——**当前为空**

按标准逐条核对，**没有模块达到"必须重写"的门槛**：

| 门槛 | 现状核对 |
|---|---|
| 设计根本错误 | 无。分层缺失/重复是**增量可解**的（C 类），不是地基错误。 |
| 安全风险无法修补 | 无。所有 🔴 洞（越权、debug_code、弱守卫、XSS）的修法都是**局部加校验/加留痕/加转义**，不需要重写模块。 |
| 无法测试 | 无。测试基础设施已存在（1344 unit + 真 PG + smoke），纯逻辑可直接特征化，已示范。 |
| 技术路线废弃 | 无。FastAPI + SQLAlchemy 2.x + Alembic 均为当前主流；免构建 SPA 是**有意约束**非废弃技术。 |
| 维护成本 > 重做 | 无。最大的债（三套子域、God 文件、倾倒场）拆分成本远低于重写，且重写会丢掉已积累的测试与并发正确性资产。 |

**最接近 D 的两个，为何仍是 C/B：**

- `app/models.py`（3950 行 God 文件）——看着像"该重写"，但它零迁移漂移、金额类型已治理、`import *` 环有测试守着；**拆包**即可，重写会破坏 246 表的迁移历史。→ **C**。
- 审计中间件哈希链（`main.py:_write_audit`）——并发下会静默分叉，像"设计错误"；但修法是加 `SELECT FOR UPDATE`/序列化隔离这类**局部**改动，链结构本身可留。→ **B（🔴 修）**，随 `main.py` 一并进 C 的拆分。

> 结论：这套代码库的正确演进节奏是 **A 不动 / B 补齐 / C 拆分收敛**，**不需要任何一处推倒重写**。
> 这既符合仓库"增量重构优先、不重写可运行模块"的规则，也避免丢失其最有价值的资产
> （并发正确性、类型治理、边界守卫、诚实文档）。

---

## 处置节奏建议（与 TECH_DEBT 的优先级对齐）

1. **先堵 🔴（跨分级）**：referral 越权、printing/attachments 越权、portal debug_code/免登录、pages-mgmt XSS、config 守卫、monitor 一行 bug、scheduler 锁误删、reporting org_id、audit 并发。
2. **C 逐块拆**：倾倒场（gapfill/service_extras）→ 统计簇 → 三套子域收敛 → God 文件分包 → 前端组件化。每块**先补特征化网再动**（见 `docs/特征化测试指引.md`）。
3. **B 顺手补**：碰到哪个路由就补 `response_model` / `paginate` / 抽 service，不专门开工。
4. **A 不碰**，只复用。
