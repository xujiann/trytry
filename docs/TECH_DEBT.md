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
| **P1-55**（新发现） | **整套用例只在收集顺序下才绿：打乱顺序实测 94 个失败、横跨 51 个文件**。根因是共享的文件型 SQLite（`test_run.db`）+ 模块级 `reset_database()`：238 个测试文件里有 168 个在自己开头**把全库 drop 掉重建**，另外 70 个一次都不调、靠继承上一个模块留下的数据跑——按字母序收集时这条链恰好接得上，换个顺序就断。失败形态正是「别人的数据」：409 冲突（行已存在）、403 越权（依赖的机构/用户是别的模块建的）、`assert 0 == 1`、空列表 IndexError。**做过对照实验**：把本轮新增的两个测试文件排除后重跑打乱顺序仍是 94 个失败，所以不是某一次新增引入的，是存量。`pytest-randomly` 只是碰巧装在本容器里、**不在 `requirements-dev.txt`**，所以 CI 与 `make verify` 都走固定顺序——这笔账一直在而没人看见。真正的代价不是「将来想并行跑」：**用例之间互相喂数据，意味着一条用例可能靠另一个模块建的行才通过**，它自己测的东西是不是真的对，无从判断。修法（未做，需单独立项）：要么让那 70 个模块各自 `reset_database()`，要么一模块一库文件；两条都得逐模块确认它原本依赖的到底是启动种子还是别人的残留 | tests/（51 个文件）|

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
| P1-35 | **23 张带 `patient_id` 却无机构列的表**永远当不了可见性依据——补机构列还是确认无需依据，需逐表业务判断（现已可量化打印，见 `test_visibility_relation_derivation.py`） | `app/visibility.py` 推导面 |
| P1-36 | **登出时"从请求取令牌"的双模回落各写了一份**（Header / Cookie），与唯一实现 `deps.token_from_request` 并行。今天不会漂：两个 logout 都由 `get_current_user`/`current_resident` 前置把过 CSRF 与准入，body 里只是重取同一枚令牌。收敛要先想清 `verify_csrf` 的语义边界 | `app/routers/auth.py:207`、`app/routers/portal.py:487-492` |
| P1-37 | **`ws.py` 自读会话 Cookie**，不走 `deps.token_from_request`——后者吃 `Request`，而 WS 握手没有 `Request`。要收敛得先把"取令牌"与"校验 CSRF"两件事拆开（这两件事今天绑在一个函数里，本身就是下一步该拆的形状） | `app/ws.py:261` |
| ~~P1-38~~ | ✅ **已关账（判定：不统一措辞，收掉走样的路）**。两套措辞**本来就该不同**：医师看的是自己要做的动作（「待接诊」——这单等我接），居民看的是自己这张单子的处境（「待接收」——医院还没接下）；统一成一句话，要么让居民端读起来像医师工作台、要么让工作台变含糊。真正的账不在用词，而在**两套措辞各存一处、各自演化**：新增一个状态码时没有任何东西提醒你居民端也要给一句话，而居民端兜底是 `.get(status, status)`——漏了不报错，**居民看到的是英文状态码**。现在两列并排长在 `referrals.STATUS_WORDING` 的同一张表里（一行一个状态、两个读者两列），`portal` 取其居民端那一列、`printing` 取业务端那一列，都不再自持副本。顺带补上了这条债背后更大的一半：**状态码集合原先散在四处**（模型默认值 / `_ALLOWED_TRANSITIONS` / `schemas.ReferralStatusUpdate` 的 pattern / 文案表），漏哪一处都不报错但后果不同——漏 pattern 会让一个业务上合法的流转被 422 挡掉，漏文案表会让界面露出英文码。闸门按四处**现算**比对，不手写清单；四种漏法各做过变异验证，另加一条把「有人顺手把居民端也改成医师措辞」拦下（结构上毫无破绽，只能靠钉住词本身） | 已关账（`tests/test_referral_status_label.py`）|
| ~~P1-39~~ | ✅ **已关账，且盲区里藏着 9 处实测放行的真缺口**。新建 `tests/test_body_org_write_guard.py`：机构外键列名从模型元数据推导（14 个），分「以谁的名义写」与「写给谁」两类——判的是**列名**（稳定）而非逐个端点，新列名未分类则 fail-closed 按 acting 处理并当场变红；「仅 `require_admin` 可达」自动豁免（`require_roles` 不算，它会放行凭权限点的自定义角色，而自定义角色不是全域角色）。分母 77 个写端点 = 已校验 54 + 自动豁免 8 + **缺口 15**。15 处全部补上 `assert_org_writable`，行为回归 `tests/test_cross_org_body_writes.py` 覆盖其中 9 条实打复现过的：乙院医师可在甲院开就诊/开处方/报传染病卡/替甲院签家医/以甲院名义开检查单·上转·申请会诊，乙院经办可从甲院药房**调出药品**、把**甲院职工**派驻出去 | 已关账 |
| ~~P1-40~~ **已清零** | **全平台孤儿端点**（948 个端点里前端找不到调用点，严格判据）。轨迹：241 → 232 → 220 → 199 → 188 → 176 → 164 → 154 → 145 → 133 → 124 → 104 → 82 → 62 → 43 → 29 → **0**。92 个路由模块全部零缺口，手写豁免 13 条（各带理由）+ 废弃端点自动豁免 2 条。**闸门不撤**：判据与三条盲区一字未改，作用从「清欠账」变成「守住零」——基线为 0 时，新加一个没有界面的端点当场变红。 | `server/tests/test_frontend_endpoint_coverage.py` |
| P1-52 | **前端渲染字段名与后端契约脱节**（写成 `i.result` 而契约是 `result_value`，界面上一列 undefined，而孤儿棘轮/转义棘轮/契约棘轮全绿）。已建闸门按 `response_model` 的 `model_fields` 现算比对，覆盖 9 条 (页面, 变量, 契约) 三元组；原先受限于近半数端点返回裸 dict（**P2-5**，不是 P1-38——后者是居民端转诊措辞，已关账），覆盖面只能随契约治理一起扩；**P2-5 已于 2026-09-21 清到 99.8%，这个限制没了**——把这道闸门从 9 条三元组扩到全部渲染函数现在是可做的，属独立任务。 | `server/tests/test_frontend_field_names.py` |
| P1-53 | **模拟诊疗的作答入口长期不存在**（`tcm_heritage` 的 simulations/attempts）。管理端注释写着「作答与评分在医师端 H5 完成」，而 `m/doctor.js` 里根本没有这块代码——两边都没建，病例维护得再全也没人练得成。已在管理端补上作答与作答记录并改正注释；**本该落在医师端 H5**（那才是医师练习的地方），迁移另案。 | `server/app/static/pages-clinical.js` / `server/app/static/m/doctor.js` |
| P1-51 | **`admin_mgmt` 与 `staffing` 两套派驻实现**（同一张 `secondments` 表）。`POST /api/mgmt/secondments/{id}/end` 已标 `deprecated`（劣化：`end_date` 必填、不校验早于开始日期、无条件把员工状态置 `active`），留用 `staffing` 那条；但 `POST /api/mgmt/secondments`（建派驻）仍在界面上使用，与 `POST /api/staffing/secondments` 并行。收敛到一套需先确认两边调用方，另案。 | `server/app/routers/admin_mgmt.py` / `server/app/routers/staffing.py` |
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
