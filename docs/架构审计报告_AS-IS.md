# 架构审计报告（AS-IS 现状）

> 审计对象：县域医共体信息化平台（medplat）+ 全域慢专病全流程管理子系统（spd）
> 审计范围：仅只读分析，未改动任何代码
> 审计方式：6 路并行专项审计（入口/配置/部署/后台任务、数据模型/迁移、路由/API、认证/授权/安全、SPD 子系统/耦合、测试/前端）+ 事实交叉核对
> 日期：2026-08-18

---

## 0. 结论速览（TL;DR）

这是一个**单进程 FastAPI 单体**，后端 47,777 行、前端 9,531 行（免构建 SPA）、测试 24,429 行，共 **246 张表 / 881 个 HTTP 端点 / 89 个路由文件 / 52 个迁移脚本**。功能覆盖面极广（覆盖国家《县域医共体信息化功能指引》36 项 + 慢专病招标 11 端 163 条）。

**核心判断：应用内部代码质量高于行业平均，架构风险几乎全部集中在两个地方——(1) 应用之外的边界层（部署产物、CI、配置守卫），(2) 按"验收条目"而非"业务边界"堆积形成的结构性重复。**

值得肯定的工程自觉度（不应在后续重构中丢失）：
- `concurrency.py` / `clock.py` / `events.py` / `scheduler.py` / `deps.py` / `spd/platform.py` 的模块文档诚实记录了踩过的坑与取舍，达到教科书水准；
- 金额列已彻底从 Float 迁移到 `Numeric(14,2)`（48 列），配套方言测试；
- 迁移零漂移（246 张模型表 100% 被迁移覆盖），52/52 迁移都实现了 `downgrade()`；
- 并发正确性有真实测试支撑（`test_stage14_concurrency.py` 用 threading 真并发 + AST 静态防复发扫描）；
- SPD 子系统的单向依赖边界由 AST 测试机器化守住，确实是单向的。

最需要优先处理的（按投入产出）：
1. **部署产物开箱即崩溃 / 公网默认口令**（compose、render.yaml）——见 §12、§14；
2. **CI 是"假绿"**（覆盖率门禁带 `|| true`、迁移从不执行、真 PG 用例永远 skip、e2e 永远 skip）——见 §13；
3. **一处已确认的存储型 XSS** + 管理端 token 明文存 localStorage——见 §9、§10；
4. **同一业务概念三套并行实现**（慢病 / 专病 / 慢专病）造成的数据孤岛——见"重复实现"章节。

---

## 第一部分：AS-IS 架构现状

### 1. 仓库结构

```
/home/user/trytry
├── server/                     # 唯一应用（FastAPI 单体）
│   ├── app/
│   │   ├── main.py             # ASGI 入口 + lifespan 启动钩子（517 行）
│   │   ├── config.py           # pydantic-settings 配置（MEDPLAT_* 前缀）
│   │   ├── database.py         # 唯一 Base + engine
│   │   ├── deps.py             # 认证/鉴权/分页/机构范围（横切）
│   │   ├── models.py           # 全部平台 ORM 模型（3950 行 / 187 张表）
│   │   ├── security.py / gmcrypto.py / audit_chain.py / privacy.py  # 安全底座
│   │   ├── visibility.py       # 横向数据隔离（461 行）
│   │   ├── scheduler.py / jobs.py / concurrency.py / clock.py / state_store.py
│   │   ├── events.py / notify.py / ws.py / sms.py / wechat.py       # 事件与通知
│   │   ├── schemas.py          # Pydantic 出入参（仅覆盖第一期 15 模块）
│   │   ├── routers/            # 80 个平台路由文件（25,059 行）
│   │   ├── data/               # 6 个种子数据模块
│   │   ├── spd/                # 慢专病子系统（独立包，13,593 行）
│   │   │   ├── __init__.py     # register_spd() 装卸入口
│   │   │   ├── platform.py     # 对平台模型/路由的唯一适配层
│   │   │   ├── service.py / rules.py / collectors.py / subscribers.py / callcenter.py / reporting.py / jobs.py / seed.py
│   │   │   ├── models.py       # 59 张 spd_* 表（1398 行）
│   │   │   └── routers/        # 9 个子系统路由（8,674 行）
│   │   └── static/             # 免构建 SPA（管理端 + m/ 居民端 + m/doctor 医生端）
│   ├── alembic/versions/       # 52 个迁移（双 head 分支：平台 + spd）
│   ├── tests/                  # 80 个测试文件（920 个测试函数）+ e2e/
│   └── scripts/                # 备份/恢复/导入/压测/演示灌数
├── docs/                       # 30+ 份中文设计与轮次文档
├── book/                       # 一本关于本系统的书稿（19 章 + 附录）
├── Dockerfile / server/Dockerfile  # 两份等价 Dockerfile
├── docker-compose.yml          # app + PostgreSQL 16 + Redis 7
├── render.yaml                 # Render 演示部署
└── README.md                   # 自述（部分数字已陈旧）
```

**观察**：单应用、单语言（Python 后端 + 原生 JS 前端）、单体部署。文档量（docs + book）与代码量相当，说明项目以"边写边记录"的方式演进，共经历了至少 17 轮开发/审阅。

### 2. 主要应用

只有一个可部署应用：`server/app`，一个 FastAPI ASGI 应用。前端是内嵌其中的三套免构建 SPA（同源 `/static`）：
- **管理端**（`/`）：58+ 页面，覆盖每个后端业务模块；
- **居民端**（`/m`）：H5 移动优先，手机验证码/微信登录；
- **医生移动工作台**（`/m/doctor`）：H5，七页签。

三套前端**代码零复用**（各自实现 `$`/`esc`/`api`），实为四套独立前端应用（管理端 + 3 个移动入口中的 2 个 HTML）。

### 3. 主要模块

后端按业务域组织（无正式 domain/service 分层）：

| 层 | 内容 |
|---|---|
| 入口/装配 | `main.py`（路由注册 + lifespan 种子化 + 三层中间件） |
| 横切基础设施 | `deps`（认证鉴权分页）、`visibility`（数据隔离）、`concurrency`（并发原语）、`events`（事件总线）、`clock`（时间）、`audit_chain`（审计哈希链）、`state_store`（Redis/内存双态） |
| 工具 | `formula`（数值 AST 求值）、`rules`（条件 DSL）、`privacy`（脱敏）、`gmcrypto`（SM3） |
| 领域 | **无独立领域层**——业务逻辑内联在 90 个路由文件中（`db.query` 在路由中出现 1003 次） |
| 子系统 | `spd/` 是唯一有局部服务层（`service.py`）的部分 |

### 4. 入口点

| 层 | 内容 | 位置 |
|---|---|---|
| ASGI 应用 | `app = FastAPI(lifespan=lifespan)` | `main.py:261` |
| 进程启动 | `uvicorn app.main:app --host 0.0.0.0 --port $PORT`（**无 `--workers`，恒单进程**） | `start.sh:4` |
| 容器 | `CMD ["sh", "start.sh"]` | `Dockerfile:10` |
| 开发 | `uvicorn app.main:app --reload` | README |

**lifespan startup 顺序**（每次进程启动同步执行）：
1. `Base.metadata.create_all(bind=engine)` 建表（`main.py:113`，**不走 alembic**）
2. 建 admin 用户（口令取 `settings.admin_password`）
3. 14 段种子数据（字典/ICD-10/药品/绩效指标/传染病/审方规则/慢病病种/DRG/质控/会计科目/spd 子系统等，幂等"只增不改"）
4. `sync_registry`（定时任务注册表进库）
5. `seed_builtin_roles` + `sync_permissions`（从路由表反射登记权限点）
6. `asyncio.create_task(scheduler_loop())` 拉起调度协程

**lifespan 无 try/except**（只有 `finally: db.close()`）——**任一段种子异常 = 整个应用起不来**。

其它入口：`/api/health`（带 DB 探活）、静态挂载、约 90 个 `include_router`、`register_spd(app)` 开关式装卸子系统。

### 5. 数据库结构

- **246 张表**（平台 187 + 慢专病 59），交叉核对与 README 自述一致。
- **主键**：246/246 统一 `id: Integer, primary_key=True` 自增代理键，零例外（无 UUID、无复合主键、无自然键）。
- **外键**：字符串式 `ForeignKey("表.id")`，平台侧平均 1.59/表，spd 侧 1.90/表。spd → 平台跨域外键 76 处（users 36 / organizations 22 / patients 18）。
- **软删除**：**无**（0 处 `is_deleted/deleted_at`），全走物理删除或 `status` 字段。
- **审计字段极不统一**：`created_at` 缺失 52 张表（含 `admissions`/`vaccination_records`/`voucher_entries`/`fund_settlements` 等台账表）；`updated_at` 仅 10/246（4%）。
- **日期存储为 `String(10)`**（79 列），`datetypes.py` 只做 Pydantic 入参校验，DB 层无 DATE 约束；同库另有 `DateTime` 列（时间戳），日期与时间戳两种存储策略并存。
- **金额已治理**：`Money = Numeric(14,2, asdecimal=False)`，48 列/28 表。剩余 Float 均为临床值/权重（无金额语义），仅 `fund_distributions`/`cost_allocation_rules` 的浮点占比参与金额推导需注意。
- **无 Text 类型**：长文本一律 `String(N)`，最长 `String(1024)`——病程记录/知情同意书正文有截断风险（PG 上是硬约束）。
- **无 Enum**：状态一律裸字符串，DB 层零约束（国产库兼容 + 口径易变，是知情决策）。
- **JSON 字段密度高**：spd 侧 60 列/30 表（51%），多对多关系被拍扁进 JSON 数组（`org_ids`/`assignee_ids`），无法索引与 join——与平台侧建关联表的做法哲学不一致。
- **索引**：平台侧复合索引近乎缺席（187 表仅 2 处 `Index`），6 张表零索引；spd 侧规范得多（59 表 7 处复合索引）。
- **PII 明文入库**：`patients.id_card`/`phone` 明文存储 + 建索引，仅在 API 出口脱敏；`gmcrypto` 在模型层零引用，无静态数据加密——拖库即全量泄露。

### 6. API

- **881 个 HTTP 端点**（方法分布 GET 425 / POST 390 / PATCH 50 / DELETE 15 / **PUT 仅 1**）。
- 域分组见下表：

| 域 | 文件数 | 端点数 |
|---|---:|---:|
| 基础平台与主数据 | 15 | 66 |
| 临床诊疗 | 19 | 164 |
| 公卫/慢病/妇幼 | 12 | 102 |
| 运营与质量管理 | 14 | 131 |
| 财务医保 | 6 | 57 |
| 决策分析 | 2 | 15 |
| 集成对接 | 2 | 18 |
| 居民端 | 1 | 32 |
| 补漏/杂项 | 3 | 57 |
| 慢专病子系统 | 9 | 239 |

- **响应契约几乎放弃 schema**：`response_model=` 仅覆盖 124/881（14%）端点；路由内联 `BaseModel` 309 个，手写 `_xxx_out()` 序列化函数 93 个。`schemas.py` 只覆盖第一期 15 模块，此后 74 个路由全部改用内联+手写。spd 的 239 个端点在 OpenAPI 中响应体几乎全空。
- **无统一响应信封**：列表 225 处裸 `return [...]`、58 处 `paginate()`、少量 `{"total":...}`；动作响应键各自发明（`created/removed/deleted/bound/success/logged_out`…）。
- **状态流转风格分裂**：`PATCH /{id}/status` vs `POST /{id}/review|arrive|down|withdraw`（RPC 风格 80 个动词端点）。
- **路径命名良好**：全英文、kebab-case、复数集合，无拼音。

### 7. 外部依赖

- **运行时依赖 13 项**（`requirements.txt`）：fastapi、uvicorn、sqlalchemy、pydantic、pydantic-settings、python-multipart、alembic、psycopg2-binary、redis、pytest、httpx、pytest-cov、qrcode。
- **全部 `>=` 下界、无上界、无 lockfile**；**测试依赖（pytest/pytest-cov）被打进生产镜像**；`playwright`/`pytest-xdist`/`pytest-asyncio` 不在清单内。
- 外部服务集成均为"双通道"（默认 stub / 生产真实现）：短信（console/http）、微信（mock/official）、外呼（manual/http）、支付网关（Mock/待接）。Redis 可选（未配置退化为进程内存）。
- **无前端依赖**（免构建，无 package.json / node_modules）。

### 8. 共享组件

- **横切能力抽象良好但推广不足**：`paginate`（用 32/89 文件）、`visibility`（48/89）、`events`（仅 2 处 publish）、`app/rules`（仅 1/5 规则场景使用）。抽象建好了却没铺开。
- `spd/platform.py` 是子系统对平台的收口适配层（模型/路由的唯一入口），设计良好。
- 前端**无共享组件**：89 个 `renderX` 函数手抄同一"面板+表单+表格+msg+onsubmit"模板；三套前端各自实现基础工具函数。

### 9. 认证与授权

- **自制 JWT**（不依赖 PyJWT）：`HMAC(SECRET, header.payload)`，校验时忽略 header 的 alg 用服务端算法重算（天然免疫 alg 混淆）。算法可选 HMAC-SHA256 或 HMAC-SM3。无 iss/aud/nbf、无 refresh token、无 kid/轮换。
- **令牌 TTL**：员工端 8 小时，居民端 7 天。吊销靠 jti 黑名单（Redis/内存）+ `User.token_valid_from` 基线。
- **密码哈希**：general 套件 PBKDF2-HMAC-SHA256 12 万轮；sm 套件是朴素 SM3 迭代 2 万轮（非 PBKDF2 结构，抗 GPU 弱）。
- **两套身份体系**：员工（`users`，用户名口令）与居民（`resident_accounts`，验证码/微信），共用同一把密钥，靠 token 的 `scope` 声明双向拒绝，边界干净。
- **6 内置角色** + 自定义角色权限点。`require_roles` 内置角色内存比较、自定义角色查库。**三处结构缺口**：权限点只覆盖写方法（GET 无法授权给自定义角色）；`require_admin` 不认权限点（配了不生效）；未被 `require_roles` 守卫的读接口对自定义角色完全放开。
- **横向数据隔离**（`visibility.py`）：机构维度（visible/stats/writable 三档）+ 患者维度（global/encounter/contract/service/referral/authorization 六类关系推导）。`director` 是全域可见角色。判定与 AccessLog 留痕绑定。**但约 13 个处理患者/机构数据的路由完全未引入 visibility**（prescriptions/referrals/chronic/infectious/printing/analytics/attachments 等）。

### 10. 数据流

- **写路径**：请求 → 三层中间件（安全头 → 请求日志 → **审计落库**）→ 路由（内联业务逻辑 + 直接 ORM）→ commit。审计中间件对每个写请求新开 Session、读上一条 entry_hash、写哈希链。
- **读路径**：路由直接 `db.query`，可见性校验（若该路由接入了 visibility）+ AccessLog 留痕。
- **事件流**：`events.py` 同进程/同事务/同步发布订阅，仅 2 个已知事件（`encounter.created`/`admission.discharged`），订阅方只有 spd 子系统。
- **实时流**：`ws.py` 进程内 WebSocket 广播（危急值/缺药），多实例下必然丢消息（需接 Redis Pub/Sub，已文档化未实现）。

### 11. 后台任务

- **调度器**：asyncio 协程 + 线程池混合。`scheduler_loop()` 每轮 `await asyncio.to_thread(tick)` 再 `sleep(30)`（固定间隔，非 cron，周期会缓慢漂移）。到期时刻落库 `ScheduledJob.next_run_at`，进程重启不漏跑。
- **10 个定时任务**：平台 6 个（慢病超期/医废滞留/合同到期/制剂临期/随访超期/验证码清理），spd 4 个（数据源同步/任务超期/报告推送/宣教派发）。靠 import 副作用注册。
- **并发保护**：Redis `SET NX EX` 分布式锁（**无 Redis 时恒获锁**，退化单实例）。业务层 `concurrency.py` 提供 7 个原子写原语，质量高。
- **状态存储**：`ScheduledJob` + `JobRun`（**JobRun 无清理任务，无界增长**）。

### 12. 配置

- pydantic-settings，`MEDPLAT_` 前缀，`@lru_cache` 单例，读 `.env` + 环境变量。
- **约 25 个配置项**。**不安全默认值**：`MEDPLAT_SECRET="dev-secret-change-in-production"`、`MEDPLAT_ADMIN_PASSWORD="admin123"`、`MEDPLAT_PORTAL_LEGACY_VERIFY=True`（免登录查档案默认开）、`MEDPLAT_SMS_PROVIDER=console`（非 prod 时回显验证码）。
- **生产守卫**：`env/environment=prod` 且 secret/password 为默认值时拒绝启动。**但守卫是黑名单式字面量比对**，被 compose 的另一个默认值（`change-me-in-production`≠`dev-secret-...`）绕过。
- **影子配置**：`MEDPLAT_REDIS_URL` 直接读 `os.environ`（绕过 Settings），写进 `.env` 无效——这是"多实例必配"四件套的总开关，却是唯一不走 Settings 的关键配置。

### 13. 测试基础设施

- **920 个测试函数 / 80 文件 / 24,429 行**，跑在**文件型 SQLite**（`test_run.db`）。
- **conftest.py 只有 45 行，不定义任何 fixture**——`client` fixture 被复制 74 次，硬编码 `admin/admin123` 登录 46 次。
- **CI（唯一 workflow）只跑 pytest**，覆盖率门禁带 `|| true`（假门禁）。**52 个迁移在 CI 中从不执行**（用 create_all 建表）；真 PG 用例（`test_postgres_real.py`）永远 skip（无 postgres service）；11 个 e2e 永远 skip（playwright 不在 requirements、未传 `--e2e`）。CI 无 lint/类型检查/安全扫描。**CI Python 3.11 vs 运行时 Docker 3.12 不一致**。
- **值得肯定**：`test_stage14_concurrency.py` 用 threading 真并发 + AST 静态防复发扫描（且明确纳入 spd 目录）；`test_spd_perf.py` 数 SQL 条数防 N+1；`test_stage12_dialect.py` 8 条方言/精度静态规则。
- **命名混乱**：41% 测试文件按开发轮次命名（stage/final_gap/modules/p0/deepen），单文件塞 5-6 个不相关业务域；测试函数 600 英文 / 301 中文双轨。
- **覆盖缺口**：49 条路由零测试引用，其中 46 条（94%）在 spd 子系统（config.py 的全部 PATCH/DELETE、assess.py 积分规则整块）。

### 14. 部署基础设施

- **两份等价 Dockerfile**（根 + server/），`python:3.12-slim`，`COPY . .`（无 .dockerignore，把 .git/tests/docs/book 全打进镜像），root 运行，无 HEALTHCHECK，`ENV MEDPLAT_SEED_DEMO=1`（演示数据设成镜像默认）。
- **docker-compose.yml**：app + postgres:16 + redis:7，命名卷，健康检查规范。**但无任何 alembic 执行步骤**；`MEDPLAT_ENV=prod` + `admin123` 默认值 → 生产守卫命中 → `RuntimeError` → `restart: unless-stopped` 无限崩溃循环。
- **render.yaml**：只设 `MEDPLAT_SEED_DEMO=1`，未设 SECRET/ADMIN_PASSWORD/ENV/DATABASE_URL → 以 dev 密钥 + admin/admin123 + SQLite（无持久盘，free plan 休眠即丢数据）在公网运行，验证码回显 + 免登录查档案接口全开。钉在 agent 生成的非默认分支上。

---

## 第二部分：问题识别

> 分级：**P0**=会造成生产事故/安全事件，须尽快处理；**P1**=结构性风险；**P2**=一致性与可维护性。

### 2.1 安全风险

| P | 问题 | 位置 |
|---|---|---|
| **P0** | **render.yaml 是公网默认口令实例**：dev 密钥（仓库明文，JWT 可任意伪造 admin）+ admin/admin123 + 验证码回显 + 免登录查档案全开 | `render.yaml` |
| **P0** | **验证码回显可被利用**：`console` provider + 非 prod → `/api/portal/auth/sms/code` 回显 `debug_code` → 任意手机号登录 → 若号码在主索引唯一命中自动实名绑定 → 读他人完整档案 | `routers/portal.py:168` |
| **P0** | **配置守卫被自家 compose 绕过**：黑名单式字面量比对，`change-me-in-production`≠`dev-secret-...` 判定为"安全"，硬编码密钥上线 | `config.py:86-100` + `docker-compose.yml:11` |
| **P0** | **打印/附件端点跨机构越权**：`GET /api/print/{exam-reports,prescriptions,exam-requests,certs}/{id}` 与 `GET /api/attachments/{id}` 仅需登录，可按 id 遍历读他院患者报告/处方/附件，**无留痕** | `printing.py:178,222,271,315`；`attachments.py:159` |
| **P0** | **SPD 转诊分级审核无机构层级校验**：`review_referral` 取出的 `level` 只写入状态列从不用于鉴权，任一 doctor 账号可单账号伪造完整转诊链 | `spd/routers/referral.py:393-432` |
| **P0** | **确认的存储型 XSS**：会计科目 code/name 未转义直插 `<option value="...">` | `static/pages-mgmt.js:248` |
| P1 | **居民端零 AccessLog**：家庭成员代管调阅他人（配偶/父母）档案完全无痕，`/mine` 也查不到 | `routers/portal.py:556-580` |
| P1 | **家庭代管单因子绑定**：目标档案无手机号时仅凭姓名+身份证号即可纳管读全部健康数据 | `routers/portal.py:682` |
| P1 | **`portal_legacy_verify` 默认开启**：免登录"健康卡号+身份证号"查档案，锁定键是被猜的身份证号本身（限流弱） | `config.py:53` |
| P1 | **横向越权覆盖率矩阵失真**：分母只统计"入参含 patient_id 的 GET"，用别的 id 取对象再取患者的接口天然不进分母，覆盖率虚高为 100% | `tests/test_stage15_horizontal.py:524` |
| P1 | **管理端 token+role 明文存 localStorage**：无 httpOnly cookie、无 CSP（`unsafe-inline`），一处 XSS = 全站管理员会话失窃 | `static/core.js:15` |
| P2 | 审计链可被末尾截断（prev 取 id 最大行，不带序号绑定）；审计链 MAC 与 JWT 复用同一密钥；登录不落审计 | `audit_chain.py:17`；`main.py:418` |
| P2 | 员工账号无停用机制（无 active 字段、无停用/删除接口），离职账号无法下线 | `models.py:62` |
| P2 | GET 端点纵向越权零测试；`MAP[x]\|\|x` 兜底未转义 4 处 | `test_authz_matrix.py:32`；`core.js:470` 等 |

### 2.2 架构风险（部署/配置/CI）

| P | 问题 | 位置 |
|---|---|---|
| **P0** | **`docker compose up -d` 开箱即崩溃循环**：prod + admin123 默认 → 守卫拒启动 + `restart:unless-stopped` | `docker-compose.yml:12` |
| **P0** | **CI 是"假绿"**：覆盖率门禁带 `\|\| true`；52 迁移从不执行（create_all 建表）；真 PG 用例永远 skip；11 e2e 永远 skip；无 lint/类型/安全扫描 | `.github/workflows/ci.yml` |
| **P1** | **create_all 与 alembic 双轨，且部署产物只走 create_all**：三个部署产物（start.sh/Dockerfile/compose）无一执行迁移；README 写 `upgrade head`（单数）在双 head 下必然失败且漏 spd 59 表；该坑已真实发生过一次（补迁移九表） | `main.py:113`；README:202 |
| P1 | **分布式锁可被误删**：`_release_lock` 无条件 DELETE 不校验持有者，任务超 300s TTL 时会删掉别实例的锁 → 同任务多实例并发 | `scheduler.py:94-97` |
| P1 | **审计中间件是全局写入串行点**：每个写请求新开 Session + 读上一条哈希 + insert，无 `FOR UPDATE`，PG 高并发下哈希链会静默分叉；且无 try/except（审计失败使业务 500） | `main.py:421-457` |
| P1 | **startup 重量级种子化**：14 段串行 + 反射权限点，无锁、无宽限、无 try/except，一条脏种子=全站不可用 | `main.py:113-246` |
| P2 | 单进程（无 `--workers`）与 README 的多实例叙述矛盾；WebSocket/黑名单/限流/调度在无 Redis 时均为进程内单点 | `start.sh:4` |
| P2 | 影子配置 `MEDPLAT_REDIS_URL` 绕过 Settings；Redis 客户端每次调用新建；镜像默认灌演示数据 | `state_store.py:13` |

### 2.3 重复实现

| P | 问题 | 证据 |
|---|---|---|
| **P0** | **同一病种在两套目录表有互不感知的重复定义**：`chronic_seed.py` 与 `spd/seed.py` 用相同 code（hypertension/diabetes）写不同表、各带一套阈值，统计口径必然对不上 | `chronic_seed.py:26` vs `spd/seed.py:19` |
| P1 | **同一业务概念三套并行表**：病种目录/入组/随访在 慢病（chronic_*）、专病（disease_*）、慢专病（spd_*）三处各建一套 | `models.py` + `spd/models.py` |
| P1 | **两套转诊 → 居民端数据孤岛**：`/api/portal/me/referrals` 与 `/api/portal/spd/referrals` 返回互不相交两份列表，同一患者看到什么取决于前端调了哪个 | `referrals.py` vs `spd/routers/referral.py` |
| P1 | **三套随访**：`followups`（慢病）/`followup_tasks`（统一中心）/`spd_followup_*`（慢专病），且"统一随访中心"未收编旧随访 | `chronic.py` / `followups.py` / spd |
| P1 | **规则引擎抽象 5 次统一 0 次**：`app/rules.py` 宣称统一四套规则，实际一套都没迁移，只新增了自己（第 5 套）；spd 又是第 6 套；`/api/rules/catalog`"全平台总目录"对 spd 数据为 0 | `app/rules.py` / `spd/rules.py` / quality / dataquality |
| P1 | **gapfill.py / service_extras.py 是倾倒场**：按验收条目号（⑭⑥⑳…）分区，7 个无关业务塞一文件，挂到已有前缀 → 6 组前缀重叠 + 鉴权分裂 | `gapfill.py`；`service_extras.py` |
| P2 | **同一前缀两套鉴权基线**：`/api/performance` 在 performance.py 限 director，在 gapfill.perf_router 仅需登录（有安全影响） | `performance.py:29` vs `gapfill.py:758` |
| P2 | 93 个手写 `_xxx_out()` 序列化（名字撞车语义各异）；患者/机构查找模板重复 64+ 次；统计口径逐字重复；89 个前端 render 手抄同一模板 | 全库 |

### 2.4 过度耦合 / 循环依赖

- **模型层导入环**：`models.py`（文件末尾）→ `spd/models.py` →（取 Money/utcnow）→ `models.py`。仅靠"import 写在文件末尾"这一位置约定断开，脆弱（上移即崩）。
- **子系统适配层反向依赖平台路由**：`spd/platform.py` import `routers.attachments` 与 `routers.portal`（service→router 方向倒置），靠延迟 import 压住。
- **子系统内部分层倒置**：`spd/jobs.py` 与 `spd/reporting.py` 反向 import `spd/routers/`（定时任务/报告层依赖路由），违背子系统自立的"业务动作放服务层"规矩。
- **路由互相 import 14 处**（含 import 私有函数 `chronic._evaluate_level`）+ 40 处函数内延迟 import 规避循环。`inpatient.discharge()` 一个函数延迟 import 3 个兄弟路由（DRG/随访/结算）。
- **总体方向**：主 app → spd 只有 4 处装卸性依赖，无循环业务依赖，单向性由 AST 测试守住（这点做得好）。

### 2.5 模块边界不清

- **无领域/服务层**：90 个路由文件承担全部业务逻辑（`db.query` 1003 次 / `db.commit` 423 次），种子常量寄生在路由文件里（startup 反向 import）。
- **"通用能力被划进可卸载子系统"**：`spd/routers/followup.py` 自陈随访/报告是全院通用能力，却随 `MEDPLAT_SPD_ENABLED=false` 一并关闭——使"无损卸载"不成立。
- **路由模块划分在 URL 空间不可见**：8 个 spd 文件共用 `/api/spd`，213 端点平铺，路径冲突由注册顺序隐式裁决。
- **`platform.py"唯一入口"名不副实**：实为"模型/路由"唯一入口，7 个基础设施模块仍被 9 个路由各自直连。

### 2.6 死代码

- `spd/platform.py:56` `PLATFORM_MODELS`（零引用）；`spd/collectors.py:117` `register_collector`（从未调用）；`spd/callcenter.py:81` `set_call_provider`（从未调用）；`spd/collectors.py:47` `collect_internal`（只 count 不写库，却注册给 HIS/EMR 真实源类型）。
- `spd/routers/workbench.py:394` `region_stats` 接收 `period` 参数从不使用（暴露在 API 契约上的假参数）；`population.py:405` `distribute_candidates` 的 `user` 参数未用（导致该接口没做机构可写校验）。
- 多处未使用 import；`emergency.py:12`/`telemedicine.py:10` 保留仅为"import 路径一致性"的死 import。
- `models.py` 中 `appointment_blacklist` 已迁移合并（非孤儿，属正常演进）。

### 2.7 命名不一致

- **表名前缀 6 套粒度**：`fd_`/`ph_`/`cssd_`/`tcm_`/`esb_`/`spd_`，而多数表无前缀。同概念不同词（followups/followup_tasks/spd_revisits；report_templates/print_templates/consent_templates）。
- **迁移文件名 51/52 中文 slug**，混杂"块N/阶段N/终审轮N/M7-M12/阶段九·五第N批"五套编号；revision id 手写伪 hex，存在 3 组仅差 1 位的近碰撞（打错一字符升到别的分支）。
- **`spd/platform.py` 遮蔽标准库 `platform`**；`spd/rules.py` 与 `app/rules.py` 各有一个同名不同类的 `RuleError`；`spd/config.py` vs `app/config.py`。
- **测试函数命名双轨**（600 英文 / 301 中文）；11 处 docstring 位置错误（写在 `assert_org_writable` 之后不再是 docstring，OpenAPI 无描述）。

### 2.8 超大文件 / 超大服务

| 文件 | 行数 | 问题 |
|---|---:|---|
| `app/models.py` | 3950 | 187 个类挤一个模块（202KB），任意两人碰模型必冲突 |
| `spd/routers/config.py` | 1547 | 16 类实体 CRUD 平铺，58 端点 |
| `spd/routers/population.py` | 1397 | 6 个业务流 |
| `app/models 之外` `spd/models.py` | 1398 | 59 表 |
| `routers/portal.py` | 1332 | 混 5 类关注点（含整套第二认证子系统） |
| `spd/routers/care.py` | 1186 | 8 个独立领域 |
| `spd/routers/followup.py` | 1122 | 通用能力被困在子系统 |
| `routers/gapfill.py` | 1123 | 7 业务/6 router 倾倒场 |
| 前端 `pages-clinical.js` | 1840 | 免构建拆分只是把 5489 行切成 5 个 1000-1800 行文件 |

前端 16 个函数超 100 行（最大 `renderSpdPatients` 189 行）；`core.js`（自称公共层）含 15 个页面函数且反向调用 pages-clinical.js 的函数。

### 2.9 缺失测试

- **spd 子系统 46 条路由零测试引用**（config 的全部 PATCH/DELETE、assess 积分规则整块、care 批量写、population 分发）。
- **并发测试跑在 SQLite**（全库写锁 + 无 MVCC），证不了 PG READ COMMITTED 的 check-then-act 竞争窗口；作者已在 `test_postgres_real.py` 承认却让它在 CI skip。
- **事务边界测试几乎不存在**（全仓仅 3 文件提及 rollback/savepoint，而书稿有专章）。
- **无迁移-模型一致性测试在 CI 执行**（改模型忘写迁移 CI 全绿）。
- **零 `async def test_`**（无 pytest-asyncio，WebSocket 异步路径只能间接覆盖）。
- 平台侧最薄：telemedicine（4 端点/3 引用）、publichealth（8/10）、monitor、knowledge、eldercare。

### 2.10 已确认的功能性 Bug（非风格问题）

- **监控失败统计恒错**：`monitor.py:79` 过滤 `status != "success"`，而写入端用 `"succeeded"`，导致 `/api/monitor/overview` 把每次成功都当失败列出。全仓唯一一处写错。
- **SPD 报告"考核得分"段落忽略机构参数**：`reporting.py:147` 接收 `org_id` 却不使用，每家机构收到相同的全域数据（同模块其他段落都正确过滤）。
- **SpdScreening"疑似"判定两处阈值不一致**：医护端 `high` 才算疑似，居民自查 `mid` 就算，同表同字段分歧。
- **SPD 转诊状态机 `station` 层级 `org_level` 永远产不出**（值域不一致，两来源写同一列）。

---

## 附：总体评价

**代码在"小处"（单个模块、单个函数、并发原语、类型治理）做得比多数同类项目好，问题几乎全在"大处"（部署边界、CI 有效性、跨模块结构、三套并行子域）。** 这是一个典型的"按验收清单驱动、逐轮补漏"演进出来的系统：功能覆盖极全，工程自觉度高（注释诚实、静态防复发扫描到位），但缺少一次"从业务边界重新组织"的整理，以及一次"让 CI 真正拦得住回归、让部署产物开箱能用"的收口。

建议的先后顺序：
1. **止血（P0 安全/部署）**：修 compose/render 默认值与守卫、验证码回显、打印/附件越权、SPD 转诊越权、pages-mgmt.js XSS；
2. **让 CI 变真**：去掉 `|| true`、加 postgres service 跑迁移与真 PG 用例、把 e2e 依赖纳入并在 CI 跑一遍、加 lint/类型/安全扫描；
3. **消孤岛（数据正确性）**：统一三套病种目录/随访/转诊的口径，至少消除居民端两套 referrals 的数据分裂；
4. **结构整理**：拆 gapfill/service_extras 回归业务前缀、引入 `app/services/`、拆 models.py 与超大路由；
5. **一致性**：统一响应信封与 schema、命名规范、测试组织。

*本报告仅描述现状与风险，未对任何代码进行修改。*
