# TECH_DEBT.md — 技术债地图

> 按优先级归集的问题清单。P0=会造成生产事故/安全事件；P1=结构性风险；P2=一致性与可维护性。
> 仅记录现状与风险，不含修改。详细上下文见 `架构审计报告_AS-IS.md`。

---

## P0 — 立即处理（安全 / 部署 / 数据正确性）

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| P0-1 | **render.yaml 是公网默认口令实例**：未设 ENV/SECRET → dev 密钥(仓库明文，JWT 可伪造 admin) + admin/admin123 + 验证码回显 + 免登录查档案全开 + SQLite 无持久盘 | `render.yaml` | 医疗数据平台公网等于无认证 |
| P0-2 | **`docker compose up -d` 开箱崩溃循环**：`ENV=prod` + `admin123` 默认 → 守卫拒启动 + `restart:unless-stopped` | `docker-compose.yml:12` | README 第一条部署命令必然失败 |
| P0-3 | **配置守卫被自家 compose 绕过**：黑名单式字面量比对，`change-me-in-production`≠`dev-secret-...` 判定"安全" | `config.py:86` + `compose:11` | 硬编码密钥上线，令牌可任意伪造 |
| P0-4 | **验证码回显可被利用**：console+非prod → `/api/portal/auth/sms/code` 回显 `debug_code` → 任意手机号登录 → 唯一命中自动实名绑定读他人档案 | `routers/portal.py:168` | 最现实的可利用链 |
| P0-5 | **打印/附件跨机构越权**：按 id 遍历读他院患者报告/处方/附件，**无留痕** | `printing.py:178,222,271,315`；`attachments.py:159` | 横向越权 + 无审计 |
| P0-6 | **SPD 转诊审核无机构层级校验**：`level` 只写状态列不用于鉴权，单 doctor 账号可伪造整条转诊链 | `spd/routers/referral.py:393` | 越权 |
| P0-7 | **确认的存储型 XSS**：会计科目 code/name 未转义直插 `<option value="...">` | `static/pages-mgmt.js:248` | 属性注入事件处理器 |
| P0-8 | **同一病种两套目录互不感知**：chronic 与 spd 用相同 code 写不同表各带阈值 | `chronic_seed.py:26` vs `spd/seed.py:19` | 统计口径必然对不上 |
| P0-9 | **CI 是"假绿"**：覆盖率门禁 `\|\| true`；52 迁移从不执行；真 PG 用例永远 skip；11 e2e 永远 skip；无 lint/类型/安全扫描 | `.github/workflows/ci.yml` | 回归拦不住 |

## P1 — 结构性风险

### 安全
| # | 问题 | 位置 |
|---|---|---|
| P1-1 | 居民端零 AccessLog，家庭代管调阅他人档案完全无痕 | `routers/portal.py:556` |
| P1-2 | 家庭代管单因子绑定（目标无手机号时仅凭姓名+身份证号纳管） | `routers/portal.py:682` |
| P1-3 | `portal_legacy_verify` 默认开启，免登录查档案，限流键是被猜的身份证号 | `config.py:53` |
| P1-4 | 横向越权覆盖率矩阵失真（分母只算"入参含 patient_id"，虚高为 100%） | `test_stage15_horizontal.py:524` |
| P1-5 | 管理端 token+role 明文存 localStorage，CSP 含 `unsafe-inline`，一处 XSS = 全站管理员失窃 | `static/core.js:15` |

### 部署 / 运行
| # | 问题 | 位置 |
|---|---|---|
| P1-6 | create_all 与 alembic 双轨，部署产物无一执行迁移；README `upgrade head` 单数在双 head 下失败且漏 spd 59 表 | `main.py:113`；README:202 |
| P1-7 | 分布式锁可被误删（`_release_lock` 无条件 DELETE 不校验持有者，任务超 300s TTL 时删别实例的锁） | `scheduler.py:94` |
| P1-8 | 审计中间件全局串行点：每写请求新开 Session+读哈希+insert，无 `FOR UPDATE`，PG 高并发哈希链静默分叉；无 try/except（审计失败使业务 500） | `main.py:421` |
| P1-9 | startup 重量级种子化，无锁/无宽限/无 try/except，一条脏种子=全站不可用 | `main.py:113-246` |
| P1-10 | JobRun 表无清理任务，无界增长 | `models.py`/`scheduler.py` |

### 重复实现 / 边界
| # | 问题 | 位置 |
|---|---|---|
| P1-11 | 同一概念三套并行表（慢病/专病/慢专病），存在数据孤岛 | models + spd/models |
| P1-12 | 两套转诊 → 居民端两份互不相交 referrals 列表 | referrals.py vs spd/referral.py |
| P1-13 | 三套随访（followups/followup_tasks/spd_followup_*），统一中心未收编旧随访 | chronic/followups/spd |
| P1-14 | 规则引擎抽象 6 次统一 0 次，`/api/rules/catalog` 对 spd 数据为 0 | app/rules vs spd/rules vs quality vs dataquality |
| P1-15 | 通用能力困在可卸载子系统：spd followup 随访/报告随 `SPD_ENABLED=false` 一并关闭 | `spd/routers/followup.py` |
| P1-16 | gapfill/service_extras 倾倒场，按验收条目号分区 → 6 组前缀重叠 + 鉴权分裂 | gapfill.py；service_extras.py |

### 测试
| # | 问题 | 位置 |
|---|---|---|
| P1-17 | spd 子系统 46 条路由零测试引用（config 全部 PATCH/DELETE、assess 积分规则整块） | spd/routers/* |
| P1-18 | 并发测试跑在 SQLite（全库写锁+无 MVCC），证不了 PG READ COMMITTED 竞争窗口 | conftest.py:7 |
| P1-19 | 事务边界测试几乎不存在（全仓仅 3 文件提及 rollback，书稿有专章） | tests/ |
| P1-20 | 74 份复制粘贴 client fixture + 46 处硬编码登录（约 700-900 行可消除） | conftest 无 fixture + 74 文件 |

## P2 — 一致性与可维护性

### 命名
| # | 问题 |
|---|---|
| P2-1 | 表名前缀 6 套粒度（fd_/ph_/cssd_/tcm_/esb_/spd_），多数表无前缀；同概念不同词 |
| P2-2 | 迁移文件名 51/52 中文 slug，混杂 5 套编号；revision id 手写伪 hex，3 组仅差 1 位近碰撞 |
| P2-3 | `spd/platform.py` 遮蔽标准库；`spd/rules` 与 `app/rules` 同名 `RuleError` 不同类 |
| P2-4 | 测试函数命名双轨（600 英文 / 301 中文）；11 处 docstring 位置错误（OpenAPI 无描述） |

### 契约 / 响应
| # | 问题 |
|---|---|
| P2-5 | `response_model=` 仅 14% 端点；309 内联 BaseModel + 93 手写 `_out()` |
| P2-6 | 无统一响应信封；动作响应键各自发明；`X-Total-Count` 三种来源 |
| P2-7 | 状态流转 3 种风格；RPC 动词 80 个；PUT 仅 1 次（孤例） |
| P2-8 | paginate 仅 32/89 文件；210 处直接 `.limit()` 会随数据量静默截断 |

### 超大文件
| 文件 | 行数 | 问题 |
|---|---:|---|
| `app/models.py` | 3950 | 187 类挤一模块，碰模型必冲突 |
| `spd/routers/config.py` | 1547 | 16 类实体 CRUD 平铺 |
| `routers/portal.py` | 1332 | 混 5 类关注点（含整套第二认证子系统） |
| `routers/gapfill.py` | 1123 | 7 业务/6 router 倾倒场 |
| `static/pages-clinical.js` | 1840 | 免构建拆分只是 5489 行切成 5 个大文件 |

前端 16 个函数超 100 行（最大 `renderSpdPatients` 189 行）；`core.js` 含 15 个页面函数且反向调用 pages-clinical.js。

### 死代码
| # | 项 | 位置 |
|---|---|---|
| P2-9 | `PLATFORM_MODELS`（零引用） | `spd/platform.py:56` |
| P2-10 | `register_collector` / `set_call_provider`（从未调用） | `spd/collectors.py:117`；`callcenter.py:81` |
| P2-11 | `collect_internal` 只 count 不写库却注册给 HIS/EMR 真实源类型 | `spd/collectors.py:47` |
| P2-12 | `region_stats` 的 `period` 参数、`distribute_candidates` 的 `user` 参数未用（后者导致缺机构校验） | `workbench.py:394`；`population.py:405` |
| P2-13 | 多处未使用 import；`emergency.py:12`/`telemedicine.py:10` 死 import | |

### 已确认功能 Bug（非风格）
| # | Bug | 位置 |
|---|---|---|
| P2-14 | `/api/monitor/overview` 把每次成功当失败（`"success"` vs `"succeeded"`） | `monitor.py:79` |
| P2-15 | SPD 报告考核段落忽略 org_id，各机构收到相同全域数据 | `spd/reporting.py:147` |
| P2-16 | SpdScreening 疑似判定两处阈值不一致（high vs mid） | `population.py:132` vs `spd/portal.py:362` |
| P2-17 | SPD 转诊 `station` 层级 `org_level` 永远产不出（值域不一致） | `referral.py:49` vs `platform.py:90` |

### 前端其它
| # | 问题 | 位置 |
|---|---|---|
| P2-18 | 三份独立 `$`/`esc`/`api` 实现，改一处另两处不跟 | core/m.js/doctor.js |
| P2-19 | 89 render 手抄同一模板，无 panel/crudPage/分页/加载态抽象 | pages-*.js |
| P2-20 | `PAGES[1]` 硬编码下标作默认页，头部插分组即崩 | `core.js:141` |
| P2-21 | 11 个 UI 状态塞 localStorage 当参数，页面不可分享/不支持前进后退/跨标签污染 | pages-mgmt.js 等 |
| P2-22 | 居民端靠正则匹配中文错误消息判断登录失效，后端改文案即失效 | `m/m.js:36` |
| P2-23 | `MAP[x]\|\|x` 兜底未转义 4 处 | `core.js:470` 等 |

### 其它
| # | 问题 |
|---|---|
| ~~P2-24~~ | ~~CI Python 3.11 vs 运行时 Docker 3.12 版本不一致~~ —— **已修**：CI 两个 job 统一走 `PYTHON_VERSION: "3.12"`，与两个 Dockerfile、ruff `target-version`、mypy `python_version` 同版；`tests/test_python_version_alignment.py` 钉住四处不许再漂 |
| P2-25 | 影子配置 `MEDPLAT_REDIS_URL` 绕过 Settings；Redis 客户端每次调用新建 |
| P2-26 | 两份等价 Dockerfile；无 .dockerignore；镜像默认灌演示数据；测试依赖进生产镜像 |
| P2-27 | README 数字陈旧（徽章 520 passed 实际 920 测试函数；7 e2e 实际 11） |
| P2-28 | 审计链可末尾截断 + 与 JWT 复用密钥；员工账号无停用机制；登录不落审计 |

---

## 不应在重构中丢失的优点

| 优点 | 位置 |
|---|---|
| 并发原语 + 3 次事故记录 | `concurrency.py` |
| AST 静态防复发扫描（含 spd 目录）：写唯一约束表必须处理冲突 | `test_stage14_concurrency.py:493` |
| 数 SQL 条数防 N+1（可进 CI） | `test_spd_perf.py:32` |
| 8 条方言/精度静态规则 | `test_stage12_dialect.py` |
| 金额定点数迁移 + 零迁移漂移 + 52/52 downgrade | models + alembic |
| spd 单向依赖边界的 AST 守卫 | `test_spd_boundary.py` |
| 诚实的模块文档（events/clock/deps/scheduler/platform） | 各模块 docstring |
| SM2/SM4 克制不实现 | `gmcrypto.py:17` |

---

## 建议处理顺序

1. **止血（P0 安全/部署）**：compose/render 默认值与守卫、验证码回显、打印/附件越权、SPD 转诊越权、pages-mgmt.js XSS、病种双写。
2. **让 CI 变真**：去 `|| true`、加 postgres service 跑迁移与真 PG 用例、e2e 依赖入 CI、加 lint/类型/安全扫描。
3. **消孤岛（数据正确性）**：统一三套病种/随访/转诊口径，至少消除居民端两套 referrals 分裂。
4. **结构整理**：拆 gapfill/service_extras 回业务前缀、引入 `app/services/`、拆 models.py 与超大路由。
5. **一致性**：统一响应信封与 schema、命名规范、测试组织。

*本文件仅记录现状与风险，未对任何代码进行修改。*
