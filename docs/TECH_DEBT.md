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
| P0-5 | **打印/附件跨机构越权**：按 id 遍历读他院患者报告/处方/附件，**无留痕** | `printing.py:178,222,271,315`；`attachments.py:159` | ✅ 已修（治理线：打印/附件全部接 assert_patient_visible+留痕，test_print_attachment_visibility.py） |
| P0-6 | **SPD 转诊审核无机构层级校验**：`level` 只写状态列不用于鉴权，单 doctor 账号可伪造整条转诊链 | `spd/routers/referral.py:393` | 越权 |
| P0-7 | **确认的存储型 XSS**：会计科目 code/name 未转义直插 `<option value="...">` | `static/pages-mgmt.js:248` | 属性注入事件处理器 |
| P0-8 | **同一病种两套目录互不感知**：chronic 与 spd 用相同 code 写不同表各带阈值 | `chronic_seed.py:26` vs `spd/seed.py:19` | 统计口径必然对不上 |
| P0-9 | **CI 是"假绿"**：覆盖率门禁 `\|\| true`；52 迁移从不执行；真 PG 用例永远 skip；11 e2e 永远 skip；无 lint/类型/安全扫描 | `.github/workflows/ci.yml` | 回归拦不住 |

## P1 — 结构性风险

### 安全
| # | 问题 | 位置 |
|---|---|---|
| P1-1 | 居民端零 AccessLog，家庭代管调阅他人档案完全无痕 | `routers/portal.py:556` |
| P1-2 | 家庭代管单因子绑定（目标无手机号时仅凭姓名+身份证号纳管） | ✅ 已修（阶段十四 E2：无手机号档案须 family_delegate 窗口授权，portal.py） |
| P1-3 | `portal_legacy_verify` 默认开启，免登录查档案，限流键是被猜的身份证号 | ✅ 已修（阶段十三 S：默认翻转 False + 生产守卫） |
| P1-4 | 横向越权覆盖率矩阵失真（分母只算"入参含 patient_id"，虚高为 100%） | ✅ 已修（阶段十四 Q1：分母扩 by-id 族 65→84，8 端点补防，覆盖率 95.2% 实） |
| P1-5 | 管理端 token+role 明文存 localStorage，CSP 含 `unsafe-inline`，一处 XSS = 全站管理员失窃 | `static/core.js:15` |

### 部署 / 运行
| # | 问题 | 位置 |
|---|---|---|
| P1-6 | create_all 与 alembic 双轨，部署产物无一执行迁移；README `upgrade head` 单数在双 head 下失败且漏 spd 59 表 | `main.py:113`；README:202 |
| P1-7 | 分布式锁可被误删（`_release_lock` 无条件 DELETE 不校验持有者，任务超 300s TTL 时删别实例的锁） | `scheduler.py:94` |
| P1-8 | 审计中间件全局串行点：每写请求新开 Session+读哈希+insert，无 `FOR UPDATE`，PG 高并发哈希链静默分叉；无 try/except（审计失败使业务 500） | ✅ 已修（阶段十四 P2：PG 咨询锁 + SQLite 进程锁 + try/except 兜底，test_audit_middleware_hardening.py） |
| P1-9 | startup 重量级种子化，无锁/无宽限/无 try/except，一条脏种子=全站不可用 | `main.py:113-246` |
| P1-10 | JobRun 表无清理任务，无界增长 | ✅ 已修（阶段十三 R：jobrun_cleanup 按保留期清理） |

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

### P1 新增（阶段十四收口时如实补登记）

| 编号 | 问题 | 位置 |
|---|---|---|
| P1-21 | 审计链无外部锚点：归档 manifest 只护"截断续验"，**末尾删除 N 条仍不可检出**；需定期把链头哈希锚定到异机/存证 | ✅ 已修（收口轮 G1：审计锚点自链文件+webhook 异机存证、verify 带锚点对账，test_audit_anchor.py 含末尾截断实证） |
| P1-22 | 附件仅 magic-bytes 校验，无病毒扫描旁路（ClamAV 异步标记未做） | ✅ 已修（收口轮 G2：clamd 零依赖客户端+扫描任务+感染件下载 410，test_attachment_avscan.py） |
| P1-23 | 前端令牌仍存 localStorage（XSS 失窃面）；改 HttpOnly Cookie 需配套 CSRF token，属机制性改造 | ✅ 已修（收口轮 G3：令牌 HttpOnly Cookie + CSRF 双提交，前端不再落 localStorage；Header 模式完全兼容，test_auth_cookie_csrf.py） |
| P1-24 | 护理执行联动/居民端押金透出/monitor 多实例集中化：三处已声明待办的接线（微信登录留痕已于收口轮接上） | ✅ 已修（G4 三项齐：护理记录挂医嘱外键+执行视图计数 test_nursing_order_link.py；居民端 `/me/deposits` 复用 billing 余额口径 test_portal_deposits.py；monitor 计数配 Redis 走 hash 汇总、无 Redis 字节不变 test_monitor_cluster_metrics.py） |
| P1-25 | spd 两处证件号模糊检索未接 PII 加密态（需 spd 依赖白名单先放行 `pii`） | ✅ 已修（G4：白名单放行 `pii` 经 platform.py 再导出 pii_filter；开态全值命中、模糊落空与平台同口径，关态字节不变，test_spd_pii_search.py） |
| P1-26 | 生产缺 Redis 仅警告不拒启（多实例下会话/锁定/限流静默降级）——是否升级为拒启属部署口径决策 | ✅ 已修（收口轮 G1：多实例特征+无 Redis 升级为拒启，test_redis_multi_instance_guard.py） |

### P1 新增（守卫补强轮：闸门自证覆盖面后暴露的存量欠账）

> 这一批**不是新引入的问题**，是三道守卫补强后第一次**被看见**的存量。
> 每条都在测试里立了只减不增的棘轮：修一条删一条，新增一条即变红。
> 本轮包只动脚本/测试/CI/文档，业务代码与迁移的修复归后续包。

| 编号 | 问题 | 位置 / 棘轮 |
|---|---|---|
| P1-27 | **迁移与模型真实结构漂移 77 处**：旧"零漂移"守卫只比对 `create_table("表名")` 的**表名集合**，从不看列/索引/唯一性/外键/可空性，77 处一处未发现。分三类：18 个 spd 唯一索引被迁移建成**非唯一**（DB 级唯一约束根本不存在，并发下照样插出两条）、14 个外键模型有迁移无（生产不做参照完整性校验）、25 列迁移可空而模型 NOT NULL（开发 SQLite 被 create_all 掩盖） | 基线 `server/tests/snapshots/schema_drift_baseline.json`；棘轮 `tests/test_schema_governance.py::test_迁移与模型的真实结构差异只减不增_*`（SQLite 档 75 处、PG 档 77 处——多出的 2 处无名唯一约束 SQLite 反射不出来） |
| P1-28 | **读-改-写的赋值形状 21 处**：旧规则只认 `obj.col += n`（`ast.AugAssign`），`obj.col = f(obj.col, n)` 一个都看不见。其中 8 处是真累加/追加（退款额累加、风险因素/药师意见字符串追加、随访日志与召回联系记录 JSON 列整体覆写），并发下后写覆盖先写；其余为幂等回填/取极值形状 | 清单 `tests/test_stage14_concurrency.py::KNOWN_READ_MODIFY_WRITE`；改法用 `app/concurrency.py` 的 `add_amount`/`take_amount` |
| P1-29 | **逻辑唯一表写入不处理冲突 5 处**：号源批量创建、住院登记（同患者两条在院记录）、结算认领、账单明细重复记账、病程记录重复书写。这些表**业务上唯一、库上无约束**——比撞 IntegrityError 更坏，是**静默写出两条** | 清单 `tests/test_stage14_concurrency.py::KNOWN_UNGUARDED_UNIQUE_WRITES` + `LOGICAL_UNIQUE_TABLES` |
| P1-30 | **并发防复发闸门覆盖面仅 27.5%**：229 个 `db.add` 写入点里，"唯一表"判据只覆盖 63 个，其余 166 个规则完全不看（未覆盖最多：`critical_actions` 5、`exchange_logs`/`followup_tasks`/`satisfaction_surveys`/`spd_screenings` 各 3）。补强前更低：87 文件 / 211 写入点 / 覆盖 42 个（19.9%），且 `app/spd/routers/config/` 整个子包（8 文件 18 写入点）从未被扫过 | 自证用例 `tests/test_stage14_concurrency.py::test_防复发闸门自证覆盖面`（每次运行打印分母，缺口显式化） |
| P1-31 | `spd/care.py:dispatch_edu_push` 里 `push.frequency = push.frequency` 自赋值（写了等于没写），疑似笔误、非并发问题，待查原意 | `app/spd/routers/care.py`；已登记于 `KNOWN_READ_MODIFY_WRITE` |
| P1-32 | **同一个不递归盲区还在另一道闸门上**：`tests/test_stage15_horizontal.py::_router_files()` 同样用 `os.listdir` 一层扫，横向越权防复发规则也看不到 `app/spd/routers/config/` 子包（8 文件）。本轮包不拥有该文件，未改；修法与 test_stage14 相同（改 `os.walk` 并补一条'必须递归'的自证用例） | `server/tests/test_stage15_horizontal.py:33` |


### 上线前缺陷猎捕（第十五轮）——48 条已复现缺陷的账本

四路并行猎捕，硬性要求"每条怀疑写探针脚本实际触发"，其中两路自建真 PostgreSQL 16
实例对照跑。**共 48 条已复现（阻断 13 / 应修 22 / 可后修 13），另约 30 条怀疑经验证
判为不成立**（后者同样留档，避免为不存在的问题重构）。

已修复的 13 条阻断见下表（本轮 F1/F4/F5/F6 四包完成 9 条，F2/F3 处理其余 4 条）：

| 编号 | 缺陷 | 实测症状 | 状态 |
|---|---|---|---|
| X-1 | Cookie 会话下审计留痕失去操作主体 | 浏览器端全部写操作记为 `anonymous`/user_id=NULL | ✅ F1 |
| X-2 | 归档任务不 fsync 就删行 | SIGKILL 下实测 274 行审计永久消失 | ✅ F1 |
| X-3 | 归档任务双跑同名截断覆盖 | 6 轮 5 轮产出损坏 gzip，120 行已删无导出 | ✅ F1 |
| X-4 | WS 不复核账号状态/令牌作用域 | 停用账号、居民令牌均可建连并收危急值 | F2 |
| X-5 | 押金退费在 PG 上非原子 | 预交 1000 退出 1200，余额 -200 | F3 |
| X-6 | 并发退款读-改-写 | 通道实退 400 元，台账记 100 | F3 |
| X-7 | 并发出院结算 | 4 张结算单 + 4 条医保记录（基金重复计入） | F3 |
| X-8 | 网关 pending 单不占额度 | 100 元账单收 300 元 | F3 |
| X-9 | 并发缴费超收 | 1000 元结算单收 5000 元 | F3 |
| X-10 | 并发冲销发药单 | 药品凭空增加（PG 20 片 / SQLite 10 片） | ✅ F4 |
| X-11 | 轮换宽限期 PII 索引单口径 | 同一身份证号建出第二份主索引 | ✅ F5 |
| X-12 | PII 索引丢失不可重建 | 索引全 NULL 后 EMPI 去重永久失效 | ✅ F5 |
| X-13 | `restore.sh` 相对路径校验 | 换 cwd 恢复第 0 步即退出 | ✅ F6 |

**这轮真正的教训不是 48 条缺陷，而是三道守卫都没在守**（已由 F6 补强，见 P1-27～P1-32）：
零漂移守卫只比对表名（真实差异 0 处被发现 → 补强后 3740 个对象比对出 75/77 处）；
AST 闸门判据只覆盖 19.9% 的写入点（本轮 4 个新 check-then-act 全落在盲区里）；
真 PG 集成档默认 skip 且"整档跳过"与"全部通过"退出码不可分辨。
**14 个包在隔离 worktree 里各自测绿、交互面无人验证**，是 48 条缺陷的共同成因。

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
| P2-18 | 三份独立 `$`/`esc`/`api` 实现，改一处另两处不跟 | ✅ 已修（治理线 ADR-0009：shared.js 唯一实现 + 守卫测试） |
| P2-19 | 89 render 手抄同一模板，无 panel/crudPage/分页/加载态抽象 | pages-*.js |
| P2-20 | `PAGES[1]` 硬编码下标作默认页，头部插分组即崩 | `core.js:141` |
| P2-21 | 11 个 UI 状态塞 localStorage 当参数，页面不可分享/不支持前进后退/跨标签污染 | pages-mgmt.js 等 |
| P2-22 | 居民端靠正则匹配中文错误消息判断登录失效，后端改文案即失效 | `m/m.js:36` |
| P2-23 | `MAP[x]\|\|x` 兜底未转义 4 处 | ✅ 已修（阶段十四 Q1：同形状实清 6 处 + test_frontend_escape_guard.py 防复发） |

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
