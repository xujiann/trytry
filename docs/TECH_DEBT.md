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
| P1-55（已建闸门，欠账 66 → 63） | **用例之间靠执行顺序互相喂数据**：逐模块倒序跑，**66/237 个模块变红**。⚠️ **本条上一版登记的根因是错的**（我写的）：当时写「共享 `test_run.db` + 70 个模块靠继承上一个模块的残留数据跑」，实测**不成立**——那 70 个里 63 个压根不碰库（纯 AST／静态守卫），只有 5 个用库而不 reset；而且**237 个模块单独跑全部通过**（3 个是整模块 skip），跨模块依赖是 **0**。真正的形态是**模块内部**：同一模块里第一条用例建机构、第二条基于它下单、第三条断言结果，从头跑到尾就全绿。代价每天都在收：**单独跑其中一条跑不起来**——`pytest tests/test_stage95_batch1.py::test_暂存点位不能当产生点用` 直接 IndexError（它要的暂存点位是前一条建的），而调试一条红掉的用例时第一件想做的事恰恰就是把它单独拎出来。第二笔代价是**用例可能为了错的理由而绿**：它断言的那行数据是别人建的，换个顺序就没有了。闸门 `scripts/check_test_order.py` + `make test-order`：逐模块**倒序**跑（不是随机打乱——倒序确定可复现、且 `pytest-randomly` 不在 `requirements-dev.txt` 里，工具不该依赖一个本仓库并不安装的插件），基线 63 只许变少，**修好了不删条目也会报红**（陈旧条目＝把口子敞着）。已修 4 个：`test_billing_deposit_alert_pushdown`（我上一轮新写的，参照系照抄了那个 `.limit(500)` 扫描上限）、`test_api`、`test_chronic_catalog`、`test_error_branches`。修法都不大：把前一条用例顺手建出来的东西改成本条自己建，或把「全库只有这一条」的断言改成「我建的这条在里面」 顺带补了 `tests/test_test_suite_hygiene.py`：全 `tests/` 不许有重名顶层定义（建这道闸门时真踩过——倒序开关另写了一个同名 `pytest_collection_modifyitems`，把「默认跳过 e2e」那个同名 hook 整个顶掉，11 个 e2e 当场去拉浏览器，无任何语法错误或警告），且倒序开关必须长在那唯一的钩子里（挪走了检查脚本就变成空转） | `server/scripts/check_test_order.py`（基线）｜`tests/conftest.py`（`MEDPLAT_TEST_REVERSE`）|

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
| ~~P1-33~~ | ✅ **已关账**：出口脱敏闸门 `tests/test_pii_output_masking_guard.py` 已建（分母两条路径并集——运行期 `app.routes` 的 response_model 展开 ∪ AST 推导的响应构造闭包；PII 列名取自 `EncryptedPII` 列类型、脱敏函数取自 `privacy.py` 里**定义**的公开函数，两份清单都不手写）。顺带修掉 7 处明文出网，其中 `POST /api/patients` 最重：它直接 `return patient`，而 EMPI 是**幂等**的——提交一个已存在的身份证号就能拿回那个人的档案，等于一个明文查询口子，两个 GET 兄弟端点一直是脱敏的。例外 10 条各带理由（呼救回拨号码、外呼工单号码、村医通讯录等按设计需要真值），违规基线清零 | 已关账 |
| ~~P1-34~~ | ✅ **已关账**：新建 `datetypes.PeriodStr` / `OptionalPeriodStr` / `is_period`（真过日历，形状用 `[0-9]` 而非 `\d` 以挡住全角），5 处裸正则改走真源，`deps._ASCII_MONTH` 复用同一个对象。守卫**扩展**既有的 `test_datestr_single_source.py` 而非新建（同一条判定不留两份）。年份 1900–2100：上界照抄 `fund.PoolIn.year` 的既有口径并顺带挡住 `9999-12` 下游算次月首日溢出 `datetime.max`；下界取 1900 而非 2000，是因为 `qc-summary?period=1999-01` 今天返回 200 且有用例钉着，跟着收紧会改合法月份的响应字节 | 已关账 |
| P1-35（23 张已逐张判过，剩 9 张真缺口） | **带 `patient_id` 却无机构列的表永远当不了可见性依据**。此前只是被打印成一行名字——数得清，却没人判断过；这片盲区里真混着该当依据的表时，表现是**医生在诊室里打不开本该看的档案，而任何用例都不会变红**。23 张现已逐张归类，四类判据各不相同且**未分类即 fail-closed 变红**（新建这样的表必须当场表态）：**6 张不构成服务**（居民账号／家庭代管／全域黑名单／满意度评价／档案更正申请／知情同意——评价那条方向还是反的：被差评的机构会因此获得调阅权）；**1 张父行即依据**（`bill_details` 必挂住院或就诊，且「父表真在推导面里」是**校验**出来的、不是写在注释里）；**5 张写入前就已需要依据**（写入接口先 `assert_patient_visible`，所以不可能是「唯一联系」）；**2 张机构在父行上、已接进判定**（`appointments`→号源机构、`spd_group_members`→群组机构；判定与清单两头同时接，不同步会出「档案打得开、人却不在清单里」）；**剩 9 张是真缺口**：机构只能从「经办人此刻挂在哪家机构」倒推，而那是猜测不是事实（平台有派驻／调动，`users.org_id` 会变，历史归属跟着飘）。正解是补机构列并在写入时落库，带迁移，属独立任务，逐张登记在 `_ORG_COLUMN_MISSING`，**只减不增** | `app/visibility.py`（四份清单）/ `tests/test_visibility_relation_derivation.py` |
| ~~P1-36~~ | ✅ **已关账（与 P1-37 同一刀）**。把 `deps.token_from_request` 拆成两半：`token_from_credentials_or_cookies`（**纯取值**：header 优先、其次会话 Cookie，收 `Mapping`、不校验不抛异常）与 `assert_csrf_double_submit`（**纯校验**），`token_from_request` 保持原签名与行为、由这两半组合而成。两个 logout 改用纯取值那半——CSRF 与准入本就由前置依赖 `get_current_user` / `current_resident` 判过，handler 里只是重取同一枚令牌去拉黑。这条「header 优先、其次 Cookie」的规则此前在**四处**各有一份；规则本身不会自己漂，漂的是**改的时候只改一处**（换 Cookie 名、加第二个 header），漏掉一份的表现是某条路径悄悄取不到令牌。⚠️ 收敛后把 CSRF 门的只剩前置依赖，所以补了两条回归锚：`test_登出自己也受CSRF双提交约束` / `test_portal_登出自己也受CSRF双提交约束`——把 logout 的 `dependencies=[Depends(get_current_user)]` 去掉，当场 200≠403 报红（实测过；跨站登出是拒绝服务级的骚扰）。防复发闸门是 `test_single_source_judgements.py` 的扫描 8 | 已关账（`app/deps.py`）|
| ~~P1-37~~ | ✅ **已关账（同上那一刀）**。`ws.py` 当初自己抄一份的理由是「`token_from_request` 只吃 `Request`，而 WS 握手没有 `Request`」——**签名收得太窄，把调用方推去自己抄**。拆出来的纯取值那半收 `Mapping[str, str]`，而 `Request.cookies` 与 `WebSocket.cookies` 都是普通映射，于是两边共用同一份。握手仍不做 CSRF 双提交（GET、无副作用、SameSite=Lax，理由在该函数 docstring 里），这正是「取令牌」与「校验 CSRF」拆开之后才说得清的边界 | 已关账（`app/ws.py`）|
| ~~P1-38~~ | ✅ **已关账（判定：不统一措辞，收掉走样的路）**。两套措辞**本来就该不同**：医师看的是自己要做的动作（「待接诊」——这单等我接），居民看的是自己这张单子的处境（「待接收」——医院还没接下）；统一成一句话，要么让居民端读起来像医师工作台、要么让工作台变含糊。真正的账不在用词，而在**两套措辞各存一处、各自演化**：新增一个状态码时没有任何东西提醒你居民端也要给一句话，而居民端兜底是 `.get(status, status)`——漏了不报错，**居民看到的是英文状态码**。现在两列并排长在 `referrals.STATUS_WORDING` 的同一张表里（一行一个状态、两个读者两列），`portal` 取其居民端那一列、`printing` 取业务端那一列，都不再自持副本。顺带补上了这条债背后更大的一半：**状态码集合原先散在四处**（模型默认值 / `_ALLOWED_TRANSITIONS` / `schemas.ReferralStatusUpdate` 的 pattern / 文案表），漏哪一处都不报错但后果不同——漏 pattern 会让一个业务上合法的流转被 422 挡掉，漏文案表会让界面露出英文码。闸门按四处**现算**比对，不手写清单；四种漏法各做过变异验证，另加一条把「有人顺手把居民端也改成医师措辞」拦下（结构上毫无破绽，只能靠钉住词本身） | 已关账（`tests/test_referral_status_label.py`）|
| ~~P1-39~~ | ✅ **已关账，且盲区里藏着 9 处实测放行的真缺口**。新建 `tests/test_body_org_write_guard.py`：机构外键列名从模型元数据推导（14 个），分「以谁的名义写」与「写给谁」两类——判的是**列名**（稳定）而非逐个端点，新列名未分类则 fail-closed 按 acting 处理并当场变红；「仅 `require_admin` 可达」自动豁免（`require_roles` 不算，它会放行凭权限点的自定义角色，而自定义角色不是全域角色）。分母 77 个写端点 = 已校验 54 + 自动豁免 8 + **缺口 15**。15 处全部补上 `assert_org_writable`，行为回归 `tests/test_cross_org_body_writes.py` 覆盖其中 9 条实打复现过的：乙院医师可在甲院开就诊/开处方/报传染病卡/替甲院签家医/以甲院名义开检查单·上转·申请会诊，乙院经办可从甲院药房**调出药品**、把**甲院职工**派驻出去（⚠️ 派驻这条当时只修了一半，见 P1-51：旧入口漏修，新入口靠谎报 `from_org_id` 可绕过） | 已关账 |
| ~~P1-40~~ **已清零** | **全平台孤儿端点**（948 个端点里前端找不到调用点，严格判据）。轨迹：241 → 232 → 220 → 199 → 188 → 176 → 164 → 154 → 145 → 133 → 124 → 104 → 82 → 62 → 43 → 29 → **0**。92 个路由模块全部零缺口，手写豁免 13 条（各带理由）+ 废弃端点自动豁免 2 条。**闸门不撤**：判据与三条盲区一字未改，作用从「清欠账」变成「守住零」——基线为 0 时，新加一个没有界面的端点当场变红。 | `server/tests/test_frontend_endpoint_coverage.py` |
| ~~P1-56~~ | ✅ **已关账：判据补上，34 处按形状扫出、26 处修、8 处书面豁免**。缺陷形态：写接口只校验**调用方自报的机构 id**，同时又按 id 引用了一个**自带机构归属**的实体——真正被写的是那个实体所属的机构，它从头到尾没被看过。P1-39 的静态闸门判的是「调没调 `assert_org_writable`」，函数名出现就算过，所以这一整类全绿。登记时手工列了 6 处；新判据（`test_body_org_write_guard.py` 的 P1-56 段）逐个**取数点**判：凡按请求体里的 id 取出带 `org_id` 列的实体（`db.get(M, body.x)` / `M.id.in_(body.xs)`，含 `for item in body.items`），函数里必须拿**这个实体自己的** `.org_id` 去比对或去校验，只认绑定名、不认「函数里某处调过守卫」。在修复前的代码上它扫出 **34 处**。挑 18 处实打**全部放行**，最重的动的是钱与实物：乙院经办从**甲院住院单退押金** 201、把甲院的住院单**结算**掉（结算单记在甲院名下）、**订甲院的手术室**、用甲院的**疫苗批次**接种（甲院批次已用量 0→1）、在甲院病区收住院、给甲院住院患者下医嘱/记账/开手术申请、`distribute_candidates` 不给 `org_id` 就**一道校验都没有**、给了本院的 `org_id` 就把**甲院目标池里的患者划进乙院**、手工建任务挂甲院的纳管档案时**从档案继承团队与责任人**（活派进甲院队列）。修法统一：按实体自己的机构判归属（`assert_org_writable(db, user, <实体>.org_id)`），或比对后 422。8 处豁免逐条写理由（上报不良反应的机构本就常常不是接种机构、跨机构取药、医共体家医团队本就跨机构组建……），陈旧豁免与理由过短都会报红。判据本身做了非空洞验证：修复前三种形状（未比对的 get、按一组 id 取出后不逐行比对、只查存在不留名）必须全认出，「校验自报字段」不能被当成比对过；在修复前的代码上跑，26 处逐一被点名。行为回归 24 条（`tests/test_cross_org_body_writes.py`），**每条都在修复前的代码上确认是红的**——其中「路径启动」那条**两次红得不对**（草稿模板、空模板先被业务校验挡掉，根本没走到归属校验），补齐前置条件后才红在 201 上。修完跑全量挂了 3 条既有用例，**全是用例自己在做跨机构写**（乡镇医生往县医院病历写病程、给县医院住院单开手术、一院医生给二院就诊写病历），改成由本机构的人来做，没有放宽任何校验 | 已关账 |
| ~~P1-57~~ | ✅ **已关账：闸门放宽两处视野后逐条研判，共修 35 处（P1-56 期间 9 + 本轮 26）、8 处书面豁免**。缺陷形态：按**路径** id 取别家实体的写接口，两道横向越权闸门都看不见——① 取数写进同文件 helper（`_get` / `_close` / `_pending` / `_resource`……），闸门只在 handler 源码里找 `db.get(M,`；② 模型的机构列不叫字面 `org_id`（`from_org_id`/`to_org_id`、`managed_by_org_id`、`center_org_id`、`dest_org_id`、`claimed_org_id`、`lead_org_id`），闸门按字面 `org_id` 找模型，整族不在视野里。闸门改为：带任一 `*org_id` 列的模型都算；handler 调了本文件里会 `db.get` 这类模型的 helper 也算取数；`_assert` 开头的同文件 helper 算守卫；仅 `require_admin` 的接口不计（admin 是全域角色，守卫恒放行）。**本轮 26 处在修复前逐条实打全部 2xx**：第三家受理/拒绝/出具意见/计费/评价别家之间的会诊；回收、作废甲院就诊凭据；给甲院的专病入组记节点、出组；替甲院的知情同意书登记签字/拒签；改甲院项目、加里程碑；改/发布/撤下甲院的共享资源；调整甲院患者的路径实例；给甲院管的慢病患者录随访（分级被改成高危）；推进甲院中心的灭菌批次；替接收医院判定抢救转归；给别家已领取的申请单出报告（危急值会以它的结论推给申请机构）；给别家之间的转诊签发医保转诊证明；改甲院牵头病种的纳管规则、加管理目标；改甲院牵头的专病中心、以甲院名义挂牌中心。修法按单据自身的机构判；**跨两家的单据**（会诊、转诊证明）新增 `visibility.assert_any_org_writable`（任一方可、第三家 403），会诊按动作分给一方（受理/拒绝/出意见＝受邀方，评价＝申请方，计费＝双方任一，不替业务猜）。撤销档案授权**按设计不拦**（撤销是患者的权利，在哪个窗口提出都得办成），补上原先缺的留痕（授权与撤销同走 `log_patient_access`，与授权清单同口径）。豁免 8 处逐条写理由：院前急救推进/体征/绿道节点 3、诊断中心领取与样本物流 2、共享中药房流转 1、慢专病转诊规则（全县配置、`target_org_id` 是去向不是归属）1，外加「只许发起人本人撤回」的转诊撤回 1（比机构判更严，单列一张表）。另有 3 条原豁免（慢专病转诊审核/到院/下转）其实早由 `_assert_holds_case`/`_assert_review_authority` 守住，随闸门认得 `_assert` helper 转为已防护、删去。闸门在修复前代码上恰好标出这 26 处。顺带修正：`PathInstanceOut.owner_user_id` 契约比可空列更严，无负责人的实例 GET/PATCH 都 500（P2-5 契约工作自己引入的回归，已补用例） | 已关账（`tests/test_stage15_horizontal.py` / `tests/test_cross_org_body_writes.py`）|
| ~~P1-58~~ | ✅ **已关账：补三列处理方机构 + 闸门认一跳归属，修 24 处越权（修复前逐条实打全部 2xx），剩 1 处书面豁免**。登记时是「跨机构单据缺处理方机构列」，研判中发现同根的更大一族：**归属在一跳之外**的表（里程碑之于项目、危急值报告之于申请单、服务包绑定之于纳管档案、团队成员之于团队……）自己没有机构列，闸门按「模型带不带 `*org_id` 列」找，整族不在视野里。① **补列**（迁移 `c3f7a9e1d5b2`，只加列不回填——三列都找不到可复算的事实来源，存量为空＝归属未定）：`emergency_cases.dispatch_org_id`（建事件时取操作人机构，院前推进/体征/绿道节点按调度方或接收医院任一判；存量事件照旧不判，只拦得住一方会把在途事件的调度方关在门外）、`tcm_dispense_orders.pharmacy_org_id`（首个推进它的**非下单机构**用条件 UPDATE 领取，与诊断中心领取申请单同形；下单方推进不领取，免得把真正承接的共享药房关在门外）、`pathology_specimens.center_org_id`（核收/拒收时落库，此前退回看申请单的 `claimed_org_id`）。② **一跳归属修 14 处**：第三家替申请机构「确认接收」「处置完毕」危急值（闭环率是质控指标，申请机构那头会以为已有人处置）；标完成/撤销甲院项目里程碑；核销甲院号源上的预约；办结甲院患者的慢专病干预；改管理目标；关掉甲院团队成员的随访权限、把人移出；给甲院的随访判质控不合格；扣光甲院患者的服务包次数、解绑。③ **病种配置按牵头机构判**（新增 `spd/service.program_lead_org`）：转诊规则与服务包自己没有机构列、只挂 `program_code`，别家能在甲院牵头的病种下建规则、给服务包改价。每处都与旁边的新建接口**同一口径**（改不比建更严：干预按患者可见性判，因为新建本就如此）。闸门：模型集合加上「外键指向带机构列的表」的一跳模型（指向机构/用户/患者主数据的外键不算）；只许全域角色调的接口（`require_roles('director')` 这类）与 `require_admin` 同样不计。放宽后在修复前代码上标出 20 处 by-id handler；另 4 处（新建规则/服务包、改服务包、登记送检）是请求体或按**字符串编码**挂的归属，闸门看不见，登记为 P1-59。豁免 1 处：取消预约（代约方常是另一家机构且未落库）。P1-57 的 4 条豁免（院前急救 3、中药房 1）与转诊规则 1 条随之转为已防护、删去。三列带**真外键**（`claimed_org_id` 当年只加了裸整数列，外键至今挂在结构漂移基线里，这里不再添一笔），SQLite 升降级往返与真 PG 集成用例均已跑通。修完全量挂 1 条既有用例（`test_metrics_drilldown` 的夹具让出报告的县院医师替乡镇院闭环危急值），改由 admin 造数，**没有放宽任何校验**。探针有一条**初版红得不对**：病理申请与干预共用一位患者，乙院开了病理单就与该患者有了真实服务关系，「办结干预」按患者可见性放行——改用两位患者后才红在 200 上 | 已关账（`tests/test_stage15_horizontal.py` / `tests/test_cross_org_body_writes.py`）|
| P1-59 | **两道横向闸门仍看不见的归属形状**（P1-58 收尾时实测）：① **按字符串编码挂的归属**——`spd_service_packages` / `spd_referral_rules` 只挂 `program_code`（不是外键），按 id 改服务包的接口闸门判不出它有主；P1-58 已人工补上校验，但同形状的新接口不会变红；② **请求体里引用带主实体**的新建类（在别家病种下建规则/服务包、往别家之间的病理申请登记送检）——P1-56 的请求体判据只认字面 `org_id` 模型，`*org_id` 与一跳模型不在视野里；③ **预约的代约方未落库**：取消预约按设计不判（乡镇替患者约县医院的号），代约机构补列后才能判"约的人或号源机构任一" | `tests/test_stage15_horizontal.py` / `tests/test_cross_org_body_writes.py` |
| P1-60 | **真 PG 集成用例有顺序依赖**（P1-58 跑 `make test-integration` 时发现，**改动前的代码上同样复现**）：装了 `pytest-randomly` 时 `test_postgres_real.py` 随种子 1~4 挂 1~3 条（种子 5、6 与 `-p no:randomly` 全绿）。典型报错是「脏库上迁移只报告不删数据」那条裸 `INSERT INTO users (...)` 没带 `status`，撞 NOT NULL——前一条用例把库留在了带新列约束的状态、它却假定库是它自己从头建的。与 P1-55 同一病根（用例不能单独成立），但 `make test-order` 默认不带集成用例，看不见它 | `tests/test_postgres_real.py` |
| P2-31 | **响应契约可以比列更严而无人报警**：`PathInstanceOut.owner_user_id` 声明 `int`，列是可空的，无负责人的路径实例一读就 500（`ResponseValidationError`）。契约棘轮只判"有没有 `response_model`"，不比对字段可空性与模型列是否一致。可考虑按 `from_attributes` 的输出模型与 ORM 列逐字段比对可空性 | `tests/test_api_contract_governance.py` |
| ~~P1-52~~ | ✅ **已关账（手写 9 条 → 推导 243 条）**。缺陷形态：写成 `i.result` 而契约是 `result_value`，界面上一列 undefined，而孤儿棘轮／转义棘轮／契约棘轮**全绿**（前者判「路径有没有人调」，后者判「插值过没过 esc」，`esc(undefined)` 也叫过了）。原先闸门只覆盖 9 条手写 (页面, 变量, 契约) 三元组，理由是「免构建前端没有解析器，数据流分析不可靠」——这话只对了一半：**任意**变量的来源确实推不出来，但有一条链窄到可以推准：`const 列表 = await api(路径)` → `table(表头, 列表, (行) => …)` 或 `列表.map((行) => …)`。按这条链推出 **243 处渲染点，覆盖 90 个 render 函数、443 个字段名**，且前端改了会自己跟着变，不需要谁记得回来加一行。推不准的**一律放弃而不是猜**，四条收紧全是被真实误报逼出来的：同名变量在块里赋过两次（`renderBilling` 的 `rows` 先押金流水、后调价历史）、`Promise.all` 里有条件元素导致 zip 错位、`.map` 的接收者其实是属性访问（`stats.groups.map`）、行变量括号不配平把**箭头形参表** `(qc, title) =>` 的第二个形参当成了行变量。链数设基线只许多不许少（判据被打断时闸门不会变红，只会安静地少看几处），另有一条盯住「回调体取不到任何字段」的空转链数量。三处变异各自转红，其中两处正是推导新覆盖到的函数 | `server/tests/test_frontend_field_names.py` |
| P1-53 | **模拟诊疗的作答入口长期不存在**（`tcm_heritage` 的 simulations/attempts）。管理端注释写着「作答与评分在医师端 H5 完成」，而 `m/doctor.js` 里根本没有这块代码——两边都没建，病例维护得再全也没人练得成。已在管理端补上作答与作答记录并改正注释；**本该落在医师端 H5**（那才是医师练习的地方），迁移另案。 | `server/app/static/pages-clinical.js` / `server/app/static/m/doctor.js` |
| ~~P1-51~~ | ✅ **已关账：旧入口废弃、界面改指新入口，顺带补上两个实测放行的跨机构派驻**。先确认调用方：建派驻的旧入口 `POST /api/mgmt/secondments` 挂在人力资源页的表单上，新入口挂在「人员下沉调度」页——**两个页面各有一张表单建同一种记录**，而旧的那张才是大多数人会点的。旧入口的要害不在返回残形，在**派驻类型不收、落库一律是列默认值 `long_term`**：`staffing` 模块口径写明「巡诊与短期支援不是下沉，混在一起统计会把指标做虚」，旧入口恰好把每一次派驻都记成长期派驻（实测）。处置：旧入口标 `deprecated`（与同模块已废弃的 `/end` 同一处境，行为除越权外不变、向后兼容，正式下线另案）；人力资源页的表单换成一行指向「人员下沉调度」页的链接（建、查、结束三件事那里齐全，不在第二个页面再抄一份表单）。🔴 **收敛前实测两处越权，都在派驻这一件事上**：① 旧入口**一道归属校验都没有**——乙院经办把甲院职工派走，201；② 新入口 P1-39 补的是 `assert_org_writable(body.from_org_id)`，校验的是**调用方自己填的字段**——照实填甲院会 403，**把它谎报成乙院就 201**，甲院的人照样被派走，台账上还记着一个假的派出机构（监测指标按派出机构统计）。两处都改成按**员工实际所在机构**（`employees.org_id`，非空列）判归属，新入口另加「派出机构须是该员工所在机构」422。回归进 `tests/test_cross_org_body_writes.py`（P1-39 那份现场），两条探针各做变异验证；其中「谎报」那条**初版红得不对**——接收方也填了乙院，旧代码先以「派出与接收机构不能相同」422 掉，根本没复现那个洞，补了第三家机构作接收方后才红在 201 上 | 已关账（`admin_mgmt.py` / `staffing.py` / `pages-clinical.js`）|
| P1-50 | **`GET /api/portal/me/referrals` 已被 `/me/referrals/all` 取代却没标 deprecated**：ADR-0003 方案 B 把平台转诊与慢专病转诊并成一份，居民端入口定的是聚合接口；本条只回平台侧那一半。已按豁免处理（写明理由），但**该不该正式标 deprecated 并给出下线时间表**需要确认有没有对接方在用——标了它会自动进废弃豁免、也会在 OpenAPI 上对外声明 | `app/routers/portal.py:1728` |
| ~~P1-41~~ | ✅ **已关账**：`GET /api/spd/teams` 加 `include_inactive`（与 `medwaste` 点位清单同口径），前端团队配置页改拉全量并给停用项加「启用」按钮。缺省清单行为不变 | 已关账 |
| ~~P1-42~~ | ✅ **已关账**：新增 `PATCH /api/medwaste/locations/{id}`，**只改名称与负责人**——归属与类型刻意不可改，改了会让历史医废记录的语义被事后改写（那批医废当时从哪个科室产生、存进哪个暂存间，不能变）。真要换类型就停用旧点位另建 | 已关账 |
| ~~P1-43~~ | ✅ **已关账**：新增 `GET /api/auth/totp`，回 `enabled/pending/required/action_needed`，**只回状态不回密钥**（密钥仅 setup 那一次返回，读接口再吐一遍等于把它变成随时可取的口令）。前端账号安全页据此显示真状态，并落实「你的角色被要求双因素却还没绑」那句提示 | 已关账 |
| ~~P1-44~~ | ✅ **已关账**：`GET /api/access-logs/stats` 加 `start`/`end`（与本模块清单端点同一口径，闭区间按自然日），返回里回显取数窗口；前端统计面板改为跟随上方查询表单的同一组时间字段——此前清单筛到上个月、下面的构成比还是全量，同一页两个口径更容易看错 | 已关账 |
| ~~P1-45~~ | ✅ **已关账**：`test_量表的键集合与未发布时的空令牌` 改用专属草稿。根因是「发布后才出二维码」那条会把共享夹具里的 draft **发布掉**，顺序一乱就先发布再断言未发布。四个种子（7/13/42/99）复验全绿 | 已关账 |

| P1-46 | **`spd/routers/assess.py::_period_range` 的季度/年度两支仍无跨模块守卫**：月份支已复用 `datetypes.is_period`，季度与年度是就近校验（平台侧没有对应类型）。`test_datestr_single_source.py` 认的是正则字面量，这两支没有字面量特征，它看不见——再出现同形状的第三处不会变红 | `app/spd/routers/assess.py:198`（缺陷本身已修，见 `tests/test_spd_assess_period_validation.py`） |
| ~~P1-47~~ | ✅ **已关账**：`WasteHandoverIn` 把 `handler_name` 放宽为可选并补二选一校验（挂档案或填姓名）。**只放宽受理面**，既有的「两个都传」请求照样合法、响应体一字不变 | 已关账 |
| P1-48 | **`GET /api/access-logs/mine` 不支持代管成员**：只回账户绑定的 patient_id，居民端家庭成员视角看不到「谁看过 TA 的档案」。要支持须后端加 `patient_id` 参数并走代管授权校验——属产品决定，前端不该替它选 | `app/routers/access_logs.py:164` |
| P1-49 | **若干用例存在模块内顺序依赖**（只减不增）。已清：`test_stage4_quality::test_adverse_event_anonymous_and_stats`（统计断言改成相对量——取基线再断言「自己造的那条进了统计」，断言一条没减，7 个种子里原本 4 个红，现全绿）。**仍在账**：`test_analytics` / `test_medical_record_qc` / `test_clinical_indicators` 的若干条，以及 `test_service_extras_split_contract::test_满意度统计的字段顺序照handler实际出键排`（后者在随机序下 HEAD 上就是红的） | `server/tests/` 多处 |

**关于 P1-33 原文里那条例外的更正**：原文要求为 `integration.fhir_patient_resource`
保留「按设计的明文导出」例外。执行包报称「该符号在仓库里不存在」——**这句是错的**，
它在 `app/routers/integration.py:988`，docstring 明写「明文导出」，正是原文说的那个东西。
但**不登记为例外这个动作是对的**，理由不同：它写的是落 `upload_dir` 的 ndjson **文件**
（省平台前置机全量对接件，由运维管控），不进 HTTP 响应体，因此根本不在「响应出口」
这道闸门的射程内。真正走接口面的出站导出是 `export_fhir_patient`，它早在 H1 整改时
就已按角色脱敏。
**记下这条是因为它的形状值得记**：结论对、依据错。若照单全收，`docs/` 里会留下一句
「那个符号不存在」的假话，而下一个人 grep 一下就能发现它在，进而怀疑整条记录。

**本轮一条环境陷阱（非仓库缺陷，但会让人追幻影）**：容器里装着 `pytest-randomly 5.0.0`，
而 `requirements*.txt` 与 CI 都没有它。它默认打乱用例顺序，本仓库的套件却是顺序相关的
（模块级 `reset_database()` + 单一共享 `test_run.db`）。跑全量回归请加 `-p no:randomly`，
否则会看到几十条与改动无关的失败。P1-45 是它照出来的一条**真** flake，值得单独修。

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
| P2-5 | `response_model=` 覆盖率 14% → **99.8%**（欠账 757 → **2**，85 个模块**全部**零欠账，棘轮 `tests/test_api_contract_governance.py` 只许调小）。剩下的 2 笔**加不了契约**（`GET /api/spd/scores-analysis` 与 `GET /api/audit/verify`）：两条分支的键序互不相容，`exclude_unset` 按声明顺序输出，一份声明给不出两种顺序；要收得先改响应字节，已在两处 handler 与治理文档里写明。基线停在 2 而不是 0——新端点漏契约会当场顶到 3 |
| **P1-54**（7 处已修 6） | **截断在筛选之前 → 筛选页静默漏报**（7 处）。形状一律是「`.limit(N)` 取前 N 条，**然后**在 Python 里按条件筛」，于是 `.limit()` 限的是**扫描范围**而不是输出条数——第 N 条之后的匹配项**根本没被看过**。后果不是「看不到全部」（那是 P2-8），而是**筛出来的结果就是错的**，少报的恰恰是最该看见的那几条。**已修 6 处**（判定下推到 SQL，上限随之变成输出上限、也就能走 `paginate` 了）：`pharmacy:expiring_drug_batches`、`knowledge:search_entries`、`vaccine_supply:list_batches`、`surveillance:list_resources`、`projects:list_projects`、`billing:deposit_alerts`——前五处的回归证据见 `tests/test_truncate_before_filter_regression.py`，每条都**把匹配项放在原硬上限之外**（放在上限之内的用例证明不了任何事），五处各自做过变异验证。`billing:deposit_alerts` 的两笔金额本就各是一句聚合，写成分组子查询外连即可下推（顺带拆掉了它的 N+1：改前每个在院患者两次聚合查询），用例（`tests/test_billing_deposit_alert_pushdown.py`）拿**改写前的算法本身**当参照，在同一份数据上比对集合与顺序；**取整必须跟着一起下推**——少一层 `round` 就会在恰好等于阈值的边界上分叉（`0.3 - 0.1` 在浮点上是 `0.19999999999999998`），这条是变异验证真抓到的，不是推想。⚠️ 本条此前写着「projects/surveillance 的判定跨表要设计」，**那是错的**：`overdue` 只用 `AdminProject` 自己的三列（里程碑只参与展示计数），`below_min`/`expired` 也全在 `EmergencyResource` 一张表上，都是可直接下推的单表条件。**剩 1 处**：`appointments:find_doctors`——截断之后按余号**重排**而不是筛，要先让排序键进 SQL，形状与前六处都不同。已就地注明，免得被人照 P2-8 的办法「顺手」改成 `paginate`——那会把漏报固化成翻页错乱 | 两处 handler 内注释 |
| P2-6 | 无统一响应信封；动作响应键各自发明；`X-Total-Count` 三种来源 |
| P2-7 | 状态流转 3 种风格；RPC 动词 80 个；PUT 仅 1 次（孤例） |
| P2-8 | **已建闸门**（`tests/test_pagination_governance.py`，此前这条债完全没人看着）。分母从路由推导：`response_model` 为 `list[...]` 的 GET 端点 285 个，四桶实测 71 已分页 / 1 半分页 / 139 静默截断 / 74 无上限 → 欠账基线 **214**，只许调小。两轮清掉 **126 个纯输出上限**，**214 → 83**（静默截断 139 → 8），随后修 P1-54 又带走 6 个扫描上限，**83 → 77**（静默截断 8 → 2）：`limit` 入参默认值取原硬上限，老调用方拿到的行一字不差，只多一个 `X-Total-Count` 响应头。第二轮的 84 个由改写脚本做，判据收到**只认三种结构保持形态**（`return <链>` / 不带 if 的推导式 / 赋名后不带 if 的推导式），正是用这条把**扫描上限**挡在外面——脚本自动留出 8 个待人工判断，其中两个恰好就是先前读代码手工认出来的那两个（判据与人的判断对上了）。刻意没动两类并写明理由：**扫描上限 7 处**（已升级为独立缺陷 **P1-54**，不只是分页问题——其中 5 处的判定已下推到 SQL，上限变成输出上限后顺势走了 `paginate`；**先修缺陷再谈分页**，反过来只会把漏报固化成翻页错乱）与**无上限那 74 个**（加默认上限会真的截断，属行为变更；按单患者收敛的清单加上限反而漏记录，全域清单才是真危险）|

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
| ~~P2-9~~ | ✅ **已不存在**：全仓库零匹配（`grep PLATFORM_MODELS` 在 app/ 与 tests/ 下都没有命中），该符号早已删除。登记表落后于现实 | 已关账 |
| ~~P2-10~~ | ⚠️ **这条登记错了**：两者都**是活的注入口**，且各有用例证明它真能用（`test_spd_p0p1.py:243` 用 `set_call_provider` 注入一个会抛的 provider、`:649` 用 `register_collector` 注册一个假 LIS）。「生产代码里没有调用」不等于「死代码」——实施期接真实院内系统就是从这两个口进来的，把它们当死代码删掉会把接入通道一起删掉。假的死代码条目比没有条目更坏：它会引来一次错误的清理 | 已订正 |
| ~~P2-11~~ | ✅ **已修**：函数改名为 `collect_encounter_probe` 并**不再注册给 HIS/EMR**，那两个源类型现在如实显示「未注册采集器」（进实施待办、监控页告警）。docstring 里写明了为什么：**「看起来是好的」比「明摆着没接」更危险**——没接会有人去接，看起来好的没人会去查 | 已关账 |
| P2-12 | **一半已修、一半仍在**：`distribute_candidates` 的 `user` 现在用上了（`assert_org_writable(db, user, body.org_id)`，P1-39 那轮补的），缺机构校验的问题已消除。`region_stats` 的 `period` **仍是受理即丢**：声明了这个查询参数但函数体一次都没读它，于是调用方传 `period=2026-09` 会**静默拿到全量**——比死代码更坏，是一个会骗人的入参。要么实现要么删掉（前端没在传，删掉对响应字节零影响）| `app/spd/routers/workbench.py` |
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
| P2-25 | 影子配置 `MEDPLAT_REDIS_URL` 绕过 Settings（**仍待办**）；~~Redis 客户端每次调用新建~~ —— **已修 2026-08-27**：`_redis_client` 按 `(url, timeout)` 复用客户端 + 显式超时 + 熔断，见 `tests/test_redis_hotpath_resilience.py` |
| P2-26 | 两份等价 Dockerfile；~~无 .dockerignore~~（已补）；镜像默认灌演示数据；测试依赖进生产镜像 |
| P2-27 | README 数字陈旧（徽章 520 passed 实际 920 测试函数；7 e2e 实际 11） |
| P2-28 | 审计链可末尾截断 + 与 JWT 复用密钥；员工账号无停用机制；登录不落审计 |
| P2-30 | **审计落库在事件循环上同步跑**：`audit_middleware` 是 `async def`（跑在事件循环上），里面直接同步调 `_write_audit`——开会话、查 `users`、算哈希链、插一行，全程不让出。实测（**SQLite、无 advisory lock**）每次 **中位 2.49ms / p90 2.77ms / 最大 5.11ms**，而它挂在**每个写请求**（POST/PATCH/PUT/DELETE）上。**生产 PG 上更重且没量到**：那条路还要先拿 `pg_advisory_xact_lock` 把审计链写入**跨实例串行化**，等于让各实例的事件循环互相排队。修法（`run_in_threadpool` 或落队列异步写）会改并发语义与审计链的顺序保证，**须走 ADR**，不适合夹带。登记时间 2026-08-27 |
| P2-29 | **13 项运行时依赖全是下界钉（`>=`），无 lockfile——库的默认值变了会静默改变生产行为。** 已实测到一例：`redis>=5.0` 下，redis-py **5.0.0 的 `socket_timeout` 默认 `None`**（出网永久挂起），**8.1.0 默认 5 秒**；同一份代码装出两种行为，而这条路在每个请求的主路径上。已就地修法是**显式写死超时**（`state_store.DEFAULT_REDIS_TIMEOUT`）+ AST 棘轮 `tests/test_outbound_timeout_guard.py`，把这一处堵死；但**"不吃库默认值"这条口径没有铺满**，其余 12 项依赖同样可能藏着这种版本敏感的默认值。彻底解法是加 lockfile（`pip-compile` / `uv lock`）并把镜像构建钉到锁上——那是独立任务，涉及发布流程，须走 ADR |

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
