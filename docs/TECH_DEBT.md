# TECH_DEBT.md — 技术债地图

> 按优先级归集的问题清单。P0=会造成生产事故/安全事件；P1=结构性风险；P2=一致性与可维护性。
> 仅记录现状与风险，不含修改。详细上下文见 `架构审计报告_AS-IS.md`。

---

## P0 — 立即处理（安全 / 部署 / 数据正确性）

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| P0-1 | **render.yaml 是公网默认口令实例**：未设 ENV/SECRET → dev 密钥(仓库明文，JWT 可伪造 admin) + admin/admin123 + 验证码回显 + 免登录查档案全开 + SQLite 无持久盘 | `render.yaml` | ✅ 已修（`render.yaml:17-18` MEDPLAT_SECRET `generateValue: true` 随机生成；`:22-23` ADMIN_PASSWORD `sync: false` 须控制台手填强口令；验证码回显默认关 `config.py:133` 且生产恒不回显 `portal.py:292`；免登录查档默认关 `config.py:124`；演示种子显式开且声明数据全虚构 `render.yaml:10-13`。残余：free 档 SQLite 仍无持久盘——该实例已明示纯演示、无真实患者数据，重启丢的只是演示数据） |
| P0-2 | **`docker compose up -d` 开箱崩溃循环**：`ENV=prod` + `admin123` 默认 → 守卫拒启动 + `restart:unless-stopped` | `docker-compose.yml:12` | ✅ 已修（三凭据全部 `${VAR:?}` 必填：`docker-compose.yml:20,22,23,57`——未设即 compose 报错退出，不再带默认值起动进崩溃循环；头注 `:3-13` 记录整改原因与用法） |
| P0-3 | **配置守卫被自家 compose 绕过**：黑名单式字面量比对，`change-me-in-production`≠`dev-secret-...` 判定"安全" | `config.py:86` + `compose:11` | ✅ 已修（改为长度+字符多样性+字符类别+占位符词表校验 `config.py:78-93`，生产不达标拒启 `config.py:240-270`；`change-me` 等词形在词表 `config.py:56-61`；守卫测试 `tests/test_prod_credential_guard.py`） |
| P0-4 | **验证码回显可被利用**：console+非prod → `/api/portal/auth/sms/code` 回显 `debug_code` → 任意手机号登录 → 唯一命中自动实名绑定读他人档案 | `routers/portal.py:168` | ✅ 已修（`sms_debug_echo` 默认 False `config.py:133`；回显须 console+显式开关+非生产三重门 `portal.py:292`，生产即便误开也恒不回显；日志明文同口径 `sms.py:54`；未设置时字段不出现在响应 `portal.py:242`） |
| P0-5 | **打印/附件跨机构越权**：按 id 遍历读他院患者报告/处方/附件，**无留痕** | `printing.py:178,222,271,315`；`attachments.py:159` | ✅ 已修（治理线：打印/附件全部接 assert_patient_visible+留痕，test_print_attachment_visibility.py） |
| P0-6 | **SPD 转诊审核无机构层级校验**：`level` 只写状态列不用于鉴权，单 doctor 账号可伪造整条转诊链 | `spd/routers/referral.py:393` | ✅ 已修（ADR-0004 机构树 parent_id 口径：`spd/routers/referral.py:250` `_assert_review_authority`、`:453` 审核处调用；`:273` `_assert_holds_case` 把校验推广到到院/下转/随访接收；回归 `tests/test_spd_flow.py:723`；平台侧转诊另有接收方校验 `tests/test_referral_status_authority.py`） |
| P0-7 | **确认的存储型 XSS**：会计科目 code/name 未转义直插 `<option value="...">` | `static/pages-mgmt.js:248` | ✅ 已修（`pages-mgmt.js:248` code/name 已全部 `esc()` 后插入 option；防复发守卫 `tests/test_frontend_escape_guard.py`） |
| P0-8 | **同一病种两套目录互不感知**：chronic 与 spd 用相同 code 写不同表各带阈值 | `chronic_seed.py:26` vs `spd/seed.py:19` | 统计口径必然对不上 |
| P0-9 | **CI 是"假绿"**：覆盖率门禁 `\|\| true`；52 迁移从不执行；真 PG 用例永远 skip；11 e2e 永远 skip；无 lint/类型/安全扫描 | `.github/workflows/ci.yml` | ✅ 已修（六项全阻断：unit+smoke `ci.yml:52-58`；真 PG service `:23-34` + integration 阻断 `:59-68` + "整档没跑即红"自证闸门 `:69-89`；覆盖率门禁无 `\|\| true`、低于阈值 `sys.exit(1)` `:97-108`；build 字节编译+迁移图 `:129-133`；ruff 阻断 `:134-136`；mypy+环境探针阻断 `:137-143`。迁移执行另有 unit 档硬门禁 `tests/test_migration_model_parity.py`。如实保留两点：pip-audit 尚为 warning 档 `:148-160`；e2e 仍默认不进 CI `:54`） |

## P1 — 结构性风险

### 安全
| # | 问题 | 位置 |
|---|---|---|
| P1-1 | 居民端零 AccessLog，家庭代管调阅他人档案完全无痕 | ✅ 已修 2026-08-28：平台居民端 + spd 患者端共 28 个读端点经 `accessible_patient(resource=...)`/`_patient(resource=...)` 统一留痕（resource 必填关键字参数，新端点忘表态即 TypeError）；主体 `resident:{account_id}` 与 AuditLog 同口径，依据 self/delegate 分本人与代管；表结构未动。`tests/test_portal_access_log.py` 33 条 + 变异验证 23 条转红 |
| P1-2 | 家庭代管单因子绑定（目标无手机号时仅凭姓名+身份证号纳管） | ✅ 已修（阶段十四 E2：无手机号档案须 family_delegate 窗口授权，portal.py） |
| P1-3 | `portal_legacy_verify` 默认开启，免登录查档案，限流键是被猜的身份证号 | ✅ 已修（阶段十三 S：默认翻转 False + 生产守卫） |
| P1-4 | 横向越权覆盖率矩阵失真（分母只算"入参含 patient_id"，虚高为 100%） | ✅ 已修（阶段十四 Q1：分母扩 by-id 族 65→84，8 端点补防，覆盖率 95.2% 实） |
| P1-5 | 管理端 token+role 明文存 localStorage，CSP 含 `unsafe-inline`，一处 XSS = 全站管理员失窃 | ✅ 已修（G3/P1-23：令牌改 HttpOnly Cookie + CSRF 双提交，localStorage 只剩非敏感 role 与 CSRF token（另保留迁移期存量令牌兜底读），口径见 `core.js:14-26` 头注；`tests/test_auth_cookie_csrf.py`。CSP `unsafe-inline` 因免构建内联脚本刻意保留，取舍记档 `main.py:498-506`——令牌已不可被 XSS 读走） |

### 部署 / 运行
| # | 问题 | 位置 |
|---|---|---|
| P1-6 | create_all 与 alembic 双轨，部署产物无一执行迁移；README `upgrade head` 单数在双 head 下失败且漏 spd 59 表 | ✅ 已修（ADR-0002 已实施：生产跳过 create_all `main.py:123-128`，守卫 `tests/test_adr0002_create_all_guard.py`；`start.sh:15` 启动前 `alembic upgrade heads`（多实例走 `MEDPLAT_MIGRATE_ON_START=0` 由发布流程单独跑，`start.sh:10-13`）；README:202 已改复数 heads） |
| P1-7 | 分布式锁可被误删（`_release_lock` 无条件 DELETE 不校验持有者，任务超 300s TTL 时删别实例的锁） | ✅ 已修（释放走 Lua"值等于本实例 token 才删"原子比对 `scheduler.py:52-57`，`_release_lock` `:125-130`；续期同口径防续别人的锁 `:59-63`；`tests/test_scheduler_lock.py` + CI 真 Redis 验 Lua 语义 `ci.yml:37-43`） |
| P1-8 | 审计中间件全局串行点：每写请求新开 Session+读哈希+insert，无 `FOR UPDATE`，PG 高并发哈希链静默分叉；无 try/except（审计失败使业务 500） | ✅ 已修（阶段十四 P2：PG 咨询锁 + SQLite 进程锁 + try/except 兜底，test_audit_middleware_hardening.py） |
| P1-9 | startup 重量级种子化**仍无锁、种子块仍无 try 兜底**（部分缓解：种子已全部幂等"只增不改"（查已有 code 再 add）；ADR-0002 后生产不再 create_all；PII 索引自检有 try 兜底 `main.py:269-274`）。残余风险：十余个种子块一步抛错即启动失败；"查-插"非原子，多实例同时对空库首启会在 unique(code)（如 `code_systems.code`，`models/core.py:195`）上撞 IntegrityError 把后到实例的启动打崩 | `main.py:121-292` |
| P1-10 | JobRun 表无清理任务，无界增长 | ✅ 已修（阶段十三 R：jobrun_cleanup 按保留期清理） |

### 重复实现 / 边界
| # | 问题 | 位置 |
|---|---|---|
| P1-11 | 同一概念三套并行表（慢病/专病/慢专病），存在数据孤岛 | models + spd/models |
| P1-12 | 两套转诊 → 居民端两份互不相交 referrals 列表 | referrals.py vs spd/referral.py |
| P1-13 | 三套随访（followups/followup_tasks/spd_followup_*），统一中心未收编旧随访 | chronic/followups/spd |
| P1-14 | 规则引擎抽象 6 次统一 0 次，`/api/rules/catalog` 对 spd 数据为 0 | app/rules vs spd/rules vs quality vs dataquality |
| P1-15 | 通用能力困在可卸载子系统：spd followup 随访/报告随 `SPD_ENABLED=false` 一并关闭 | `spd/routers/followup.py` |
| P1-16 | gapfill/service_extras 倾倒场，按验收条目号分区 → 6 组前缀重叠 + 鉴权分裂 | ✅ 已修（ADR-0006 拆解落地：`routers/gapfill.py` 与 `routers/service_extras.py` 均已不存在，路由回归业务前缀） |

### 测试
| # | 问题 | 位置 |
|---|---|---|
| P1-17 | spd 子系统 46 条路由零测试引用（config 全部 PATCH/DELETE、assess 积分规则整块） | ✅ 已修（tests/ 现有 **19 个 `test_spd_*` 专档**、44 个测试文件引用 spd；当年点名的两块缺口均有专测：config 域改/删分支 `test_spd_config_admin.py` + 三份 config 契约档，考核取数与积分兑换 `test_spd_assess_metrics.py`） |
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

### 第十六轮（三条工程判断平台化）新登记

| 编号 | 问题 | 位置 |
|---|---|---|
| P1-33 | **出口脱敏无守卫**：`privacy.py` 明写"新增返回身份证号/电话的接口必须复用本模块"，实际只有 2 处引用、无任何闸门。改造路径已验证可行：从响应模型含 `id_card`/`phone` 的端点推导集合，再要求函数体走 `desensitize`/`mask_*`，例外按好清单形态逐条写理由（`integration.fhir_patient_resource` 是按设计的明文导出，需保留例外） | `app/privacy.py:7` |
| P1-34 | **月份口径正则不校验日历**：`\d{4}-\d{2}` 放行 `2026-13`，5 处（fund/admin_mgmt×2/quality/reports）。与 D-3 假日期同族，需新建 `PeriodStr` 类型 | `app/routers/fund.py:224` 等 |
| P1-35 | **23 张带 `patient_id` 却无机构列的表**永远当不了可见性依据——补机构列还是确认无需依据，需逐表业务判断（现已可量化打印，见 `test_visibility_relation_derivation.py`） | `app/visibility.py` 推导面 |
| P1-36 | **登出时"从请求取令牌"的双模回落各写了一份**（Header / Cookie），与唯一实现 `deps.token_from_request` 并行。今天不会漂：两个 logout 都由 `get_current_user`/`current_resident` 前置把过 CSRF 与准入，body 里只是重取同一枚令牌。收敛要先想清 `verify_csrf` 的语义边界 | `app/routers/auth.py:207`、`app/routers/portal.py:487-492` |
| P1-37 | **`ws.py` 自读会话 Cookie**，不走 `deps.token_from_request`——后者吃 `Request`，而 WS 握手没有 `Request`。要收敛得先把"取令牌"与"校验 CSRF"两件事拆开（这两件事今天绑在一个函数里，本身就是下一步该拆的形状） | `app/ws.py:261` |
| P1-38 | **居民端转诊状态另有一套措辞**（待接收/已接收/已完成 vs 业务端 待接诊/已接诊/已结案）。这是对外分叉、不是拷贝，但两套文案会各自演化；是否统一属另案（`tests/test_portal_referral_frontend.py:41` 记着这条）。业务端与打印件之间那份逐字拷贝已在本轮合掉 | `app/routers/portal.py:1229` |

**本轮验证不成立、不予登记的一条**：J1 报"`integration.fhir_observation` 无任何鉴权依赖，只认 `X-Source-System` 头"。
实测不成立——该端点无令牌访问返回 **401**，同文件另两个入站端点同样 401。原因是鉴权挂在
`APIRouter(dependencies=[Depends(require_roles("operator"))])` 的**路由器层**（`app/routers/integration.py:62-66`），
而 `require_roles` 内部 `Depends(get_current_user)`；只读函数签名看不见它。
按本仓库的规矩，怀疑不值钱、触发才算数：跑一次探针比读一遍签名可靠，**"某处没写"不等于"某处没有"**。

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
| P2-24 | **同一缺陷的第二种写法**：`const [text] = MAP[x] \|\| [x, ""]` 之后 `${text}` 裸插——P2-23 那条正则只认行内式，一条都抓不到 | ✅ 已修 2026-08-26（按形状全仓库扫出 **33 处**，五个文件；`test_frontend_escape_guard.py` 补第二条守卫，含三种"拼写绕过"的反证用例） |
| P2-25 | 裸 `${MAP[key]}` 取单值、查不到时页面上显示字面量 `undefined` | ☐ **待办**：`core.js:809` `${CENTER_NAMES[r.center_type]}`、`core.js:609` `${WT[w.waste_type]}`、`pages-clinical.js:1875` `${SITE[r.infection_site]}` 等。与 P2-23/24 是同一族（查表没兜底），但**只是显示缺陷不是崩溃也不是 XSS**，故没并进那两条守卫——一条守卫混两种严重度，迟早因噪声被加豁免。安全写法仍是仓库自有的 `esc(MAP[x] \|\| x)`。**需要先逐处定文案**（显示原始码？显示"—"？），不是纯机械替换，故单列一项 |
| P2-26 | 33 处状态标签在手工重复 `spdTag()` 已经做对的事 | ✅ 已修 2026-08-26：抽 `statusTag(map, key)` 进 `shared.js`（**三端共用**——`.tag` 的标记契约三套前端逐字相同，与 `.panel` 那种管理端独有的不一样），**34 处**调用点收敛，`spdTag`/`spdTagOf` 两份逐字相同的实现改为委托。等价性由 `scripts/statustag_equiv.js` 在输入矩阵上逐字符证明（含数字状态码、空串、null、XSS 载荷），10 个页面渲染字节比对一致 |

### 其它
| # | 问题 |
|---|---|
| ~~P2-24~~ | ~~CI Python 3.11 vs 运行时 Docker 3.12 版本不一致~~ —— **已修**：CI 两个 job 统一走 `PYTHON_VERSION: "3.12"`，与两个 Dockerfile、ruff `target-version`、mypy `python_version` 同版；`tests/test_python_version_alignment.py` 钉住四处不许再漂 |
| P2-25 | ~~影子配置 `MEDPLAT_REDIS_URL` 绕过 Settings~~ —— **设计决定已记档 2026-08-28**：CLAUDE.md §0/§3 明文把它列为 config.py 唯一真源的记录在案例外（"MEDPLAT_REDIS_URL 例外，直接读 os.environ"）；`config.py:279-280`、`:308-310` 把该口径引为既定处理方式（MEDPLAT_SEED_DEMO/WORKERS 同法）；读取点 `state_store.py:67`。不再算待办，要收编进 Settings 属另案决策；~~Redis 客户端每次调用新建~~ —— **已修 2026-08-27**：`_redis_client` 按 `(url, timeout)` 复用客户端 + 显式超时 + 熔断，见 `tests/test_redis_hotpath_resilience.py` |
| P2-26 | 逐子项（2026-08-28 实核，状态不同分开记）：① 两份等价 Dockerfile **仍在**（根/ 与 server/ 各一份，差异仅 COPY 上下文两行，同构靠互指注释与 `tests/test_python_version_alignment.py` 钉版本——保留）；② ~~无 .dockerignore~~（已补，根与 server/ 各一份）；③ ~~镜像默认灌演示数据~~ —— ✅ 已修（A8：两个 Dockerfile 不再 `ENV MEDPLAT_SEED_DEMO=1`（根 `Dockerfile:19-21`、`server/Dockerfile:18-19`），compose 默认 0（`docker-compose.yml:26-29`），生产开种子直接拒启 `config.py:291-295`）；④ 测试依赖进生产镜像 **仍在且换了形态**：两 Dockerfile 改装 `requirements.lock`（各自 `:8-9`），而 lock 是干净 venv 全量 freeze，含 `pytest==9.1.1`/`pytest-cov==7.1.0`/`coverage==7.15.4`（`requirements.lock:23,40,41`）——测试工具随 lock 进镜像 |
| ~~P2-27~~ | ~~README 数字陈旧（徽章 520 passed 实际 920 测试函数；7 e2e 实际 11）~~ —— **已修 2026-08-28**：徽章与正文更新为实测 2546 passed + 30 skipped、e2e 实数 11（`tests/e2e/test_flows.py` 11 个用例）、管理端页面 58→91（`app.js` 注册表实数）；覆盖率陈述改为"门禁 ≥70% 强制阻断"口径，删去早已不存在的 `\|\| true` warning 模式说法 |
| ~~P2-28~~ | ~~审计链可末尾截断 + 与 JWT 复用密钥；员工账号无停用机制；登录不落审计~~ —— **已修**（四项分见：末尾截断 → P1-21 锚点 ✅，`tests/test_audit_anchor.py` 含末尾截断实证；密钥复用 → 审计链改 `signing_key("audit")` 域分隔派生不再与 JWT 共钥（`audit_chain.py:11-12,33`，派生与轮换回退 `security.py:59-89`、`tests/test_ops_key_rotation.py`）；账号停用 → ADR-0011 `users.status` active/disabled 每请求校验（`models/core.py:39`）；登录留痕 → `login_logs` 表 + record_login_event（`models/core.py:116`，ADR-0011）） |
| ~~P2-30~~ | ~~审计落库在事件循环上同步跑~~ —— **已修 2026-08-28（ADR-0016）**：`audit_middleware` 两条路径改 `await run_in_threadpool(_write_audit, …)`，事件循环不再陪等（登记时实测单次中位 2.49ms×每写请求）；本请求仍等落库完成才返回，"响应返回时已尝试落库"与吞异常两条保证一字不变，串行化锁原样复用。AST 钉：不得直调 + 必须 await（丢 await 即静默全丢），见 `tests/test_audit_middleware_hardening.py`，两处变异验证 |
| ~~P2-29~~ | ~~13 项运行时依赖全是下界钉（`>=`），无 lockfile~~ —— **已修（A7 批次，ADR-0017 补记决策）**：`requirements.lock`（干净 venv freeze 全钉版含传递依赖）+ 两个 Dockerfile 与 CI 全部改装 lock + `pip-audit` 扫 lock。真源仍是 `requirements.txt` 区间；lock 是可复现快照。守卫 `tests/test_dependency_lock.py`：直接依赖必须全部入锁、锁必须全钉版、四处安装点必须走锁。redis 超时一例的就地修（`DEFAULT_REDIS_TIMEOUT` + AST 棘轮）保留不动 |

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
