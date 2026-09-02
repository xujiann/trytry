# ROADMAP

> 今天做什么，从这里挑。每日工作流见 `docs/日常开发工作流.md`。
> 排序：先安全止血（🔴），再让 CI 变真，再逐块治理/重构。**别自己另起题目——路线没有的，先加进来。**
> 来源与依据：`docs/TECH_DEBT.md`、`docs/模块分级_KEEP_IMPROVE_REFACTOR_REPLACE.md`、`docs/adr/`。

状态：☐ 待办 · ◐ 进行中 · ✅ 完成

---

## Now（本周期，优先）

### 🔴 安全止血（P0，独立于分级，尽快）
- ✅ 生产凭据守卫改为**强度校验**（长度 + 不同字符数 + 字符类别 + 占位符词表），部署文件弱默认值清零。原守卫只比对「是否等于代码默认值」，实测两处漏网：`docker-compose.yml` 注入的 `change-me-in-production` **不等于**代码默认的 `dev-secret-change-in-production`，而该文件里写着 `MEDPLAT_ENV: prod`——`docker compose up` 不设任何变量也能起来，起来的是一个用**仓库里人人可见的字符串签 JWT** 的生产实例；`MEDPLAT_SECRET=x` 一字符同样放行。compose 三个凭据改 `${VAR:?}` 形态（没设就报错退出），`render.yaml` 用 `generateValue: true` 让平台生成随机值。回归 `test_prod_credential_guard.py` 十八条，两处变异验证。
- ✅ `routers/portal.py:168` `debug_code` 回显收紧为显式开关（新增 `sms_debug_echo` 默认关 + 生产双重门；回归测试 `test_portal_auth.py` 两条）。
- ✅ 打印 4 端点 + `attachments` 下载/列举/上传补归属校验与 `AccessLog`。口径已定：附件按 owner_type 声明 `scope`（exam_report/spd_task=患者 · adverse_event=机构 · course_material=**全员**）；`scope` 无默认值，漏声明在装载期 TypeError 炸掉。顺带补 `exam_requests.claimed_org_id`（迁移 a4c8e2f60b19，带回填）——共享诊断中心与患者的服务关系此前在模型里没有落点，中心医师写完报告打不开自己写的那份（/review 提出并已复现）。回归 `test_print_attachment_visibility.py` 十条，两处变异验证。
- ✅ `spd/routers/referral.py` 转诊审核补机构层级校验（口径 1：按机构树 `parent_id`，见 ADR-0004）：`review` 仅本单当前机构的**直接上级**可推进，全域角色放行；回归 `test_referral_review_requires_parent_org`。**后续（另案）**：① 机构树 `parent_id` 缺陷可见 —— ✅ `GET /api/organizations/tree-health` 体检接口（列 orphans + broken_chains + `referral_ready`，`test_org_tree_health.py`）；☐ 运行期"补齐"parent_id（需真实机构关系，属数据/运维口径，不自动种子）；② ✅ `_NEXT` 已收敛为村→乡→县三级（ADR-0005，存量 station_reviewed 兼容续走）；③ ✅ 机构校验已推广到 `arrive`/`down`/`receive-followup`（`_assert_holds_case`：本单当前持有机构才能操作，全域放行；回归 `test_referral_arrive_down_receive_require_current_org`）。
- ✅ `static/pages-mgmt.js` 会计科目等 `<option>` XSS 转义（含就近同类 4 处：会计科目 code/name、监测域 domain×2、流程定义 key）。

### 🚀 正式上线前（收口中）

> 这一节记录「割接前必须解决」的口子。共同点是**平时完全看不出来，偏偏在最需要它们
> 的时刻失效**——所以只能靠主动找，等它自己暴露就是在生产上暴露。
> **凡是本容器内做不完的，写清卡在哪、需要谁**，别用"已完成"糊过去。

**已做（本轮）**
- ✅ 割接前配置体检脚本 `server/scripts/preflight_check.py`：把十几项「缺失时不拒启」
  的配置一次列全（退出码 0/1/2 + `MEDPLAT_PREFLIGHT_ACK` 认账机制）。
- ✅ 容器生命周期：`start.sh` 生产路径改 `exec "$@"`（此前 SIGTERM 到不了 uvicorn，
  滚动更新每次都是 SIGKILL 硬杀）；补两份 `.dockerignore`。
- ✅ 三个跨机构写接口补归属校验：停医嘱 / 修订报告 / 支付退款。
  ⚠️ **同时暴露了守卫的分母问题**——既有横向越权门禁诚实地报 95.5%，但它的分母
  **结构性地排除了「归属要经一跳外键才看得到」的对象**，这三个洞正因此从未被报出来。
  扩大分母是独立任务，未做。
- ✅ 生产渠道硬门：微信 `official` 缺 appid 不再静默退回 Mock（那是认证绕过）；
  生产不受理无真实网关的线上支付（否则会把单据标成已支付而实际未收到款）；
  配置枚举值拼错一律拒启，不再静默走默认分支。
- ✅ 备份/恢复主线三脚本补自动化检查（此前连语法检查都没有）。
- ✅ **可观测性与出网韧性四条**（本次提交）：监控计数的超时/复用/熔断；未捕获异常
  计入错误率并带 X-Request-ID；`MEDPLAT_LOG_FILE` 覆盖全部 18 个 logger；
  `/api/health` 库不通回 503。详见提交说明与 `docs/运维手册.md`。
- ✅ **spd/care 专业侧界面补齐（2026-08-27，用户裁定"根据建议完成开发"）**：
  此前 22 条路径 / 31 个端点全部无前端调用，而需求对照表把它们逐条写成了
  成员端/个案管理师端/中心端承诺条目的实现依据——后端交付了、界面缺失。
  现补齐：管理端新增「服务团队成员端」（监测录入与趋势/量表逐题评估与统计/
  干预模板与批量干预/宣教推送与成效/异常上报与处置）与「个案管理师端」
  （在线咨询应答·转随访/复诊计划看板·邀约留痕/健康处方）两页；中心端补
  上报任务配置面板；居民端新增「咨询」页签（发起/续聊/查看对话）——**咨询两端
  一次接通**，医生工作台那个恒为 0 的"待回复咨询"计数从此有门可进。
  `measurements/batch` 一条按需求语义豁免（设备/HIS 接入路径，不需要界面）。
  取证：`render_diff.js` 实渲三页（新页非空无假绿、中心页差异恰好只从新面板
  起始字符开始）、`--dump` 证 24 处 XSS 载荷全部转义；棘轮
  `tests/test_spd_care_frontend_coverage.py` 把"每个 care 端点必须有前端调用点
  或书面豁免"钉死（分母从路由对象现算，变异验证：删一个调用点即红）。

**本容器内做不完（需要外部资源，不是代码问题）**
- ☐ **RPO≤15min 未达标**：需要一套开了 WAL 归档的真 PostgreSQL 才能实测归档延迟。
  `tests/test_ops_wal_pitr_scripts.py` 只验脚本形状，验不了指标。
  文档已按「这是路线不是能力」标注，别当成已具备。
- ☐ **四个外部通道未做过真连通**：短信网关 / 微信公众号 / 支付网关 / 电子证照。
  各自都有 Mock 与生产硬门，但**没有一次真实握手记录**。需要四套厂商测试账号。
  **准备工作已就绪**：`scripts/channel_smoke.py`——拿到账号当天在目标环境跑一遍
  即出可入档的验收记录（微信=拉 access_token 验凭据+IP 白名单；支付=签名拉当日
  流水，只读不动钱；短信须显式 `--sms-phone` 才真发，拒绝擅发；电子证照如实报
  「平台侧尚无对接代码」）。无凭据必 SKIP 绝不假 PASS，形状由
  `tests/test_channel_smoke.py` 六条锁住。
- ☐ **信创栈未验**：达梦/金仓/麒麟上一次都没跑过。需要对应环境。

**本轮量到但没修（需 ADR，不适合夹带）**
- ✅ **审计落库移出事件循环**（P2-30 → **ADR-0016**）：`audit_middleware` 两条
  路径改 `await run_in_threadpool(_write_audit, …)`。事件循环不再陪等（登记时
  实测单次中位 2.49ms × 每个写请求）；**本请求仍等落库完成才返回**——"响应
  返回时审计已尝试落库"与吞异常两条既有保证一字不变，串行化锁（PG advisory /
  SQLite 进程锁）原样复用。fire-and-forget 与队列批量写被否：省 2.5ms 的代价
  是审计链在崩溃窗口静默丢失。AST 钉（不得直调 + 必须 await）进
  `test_audit_middleware_hardening.py`，两处变异各自转红；既有 6 条硬化用例
  （并发链完整、故障注入不拖业务）原样通过。
- ✅ **依赖锁定补决策记录与防腐化守卫**（P2-29 → **ADR-0017**）：lock 本体与
  四处安装点在 A7 批次已落地，本轮补上登记时要求的 ADR，以及守卫
  `test_dependency_lock.py` 三条：直接依赖必须全部入锁 / 锁内必须全钉版 /
  安装点必须走锁不得旁路（第三条最阴——旁路后本地与 CI 照绿，只有生产漂移）。
  三处变异验证。无 `--hash` 供应链校验是记档接受的缺口，日后可在 lock 之上
  换 pip-compile 增量补。

**卡在业务裁定（不是技术问题，不应由实现方替客户决定）**
- ☐ **ADR-0014 病种目录收敛**：慢病口径随访 90 天、慢专病治疗期 30 天，同一个高血压
  患者两个数字都进各自完成率的分母。合表当天必须二选一，**选谁都会让某一批机构的
  完成率当场变化**。需要卫健口径裁定，代码侧不动。
- ☐ **居民端是否提供平台侧随访视图**：属于加功能，不是补缺口，未擅自做。
- ✅ **已按默认路径回退（2026-08-27，用户批准"根据建议完成开发"）**：共享诊断中心
  计分曾同时挂着「需卫健口径支持」与「已实施（2026-08-22）」两句互斥记录，且查不到
  批复留痕。按治理默认（影响资金分配的口径不由实现方拍板）回退到"只按申请方计分"：
  `performance.py` 计分行退回单侧，`remote_exams_provided` **保留展示**（正是"中心
  工作量被看见但没算分"的哨兵，恢复了 2026-08-22 之前的语义），响应字段不增不减、
  非中心机构字节不变。两条哨兵用例（`test_中心出报告暂不改变得分_待卫健批复` /
  `test_互认计入申请方_中心出报告量可见但不入计分`）钉住回退态。
  **待卫健批复后恢复**：`performance.py` 端点 docstring 写明恢复点（一行 + 两条用例翻转）。

### 一行/小修（童子军级，碰到即修）
- ✅ `routers/triage.py` 的 `triage_suggest` 注入了 `db: Session = Depends(get_db)`
  却**一次都没用**——每次调用白白从连接池借还一次连接。看着像是「知识库将来落表」
  的占位（模块 docstring 明写硬编码 KB 是现状不是设计），但占位不该真开会话。
  **已修（陈账，2026-09-02 核实销账）**：签名里早已没有 `db`，函数 docstring 记着
  "没有 db 形参是有意的"及原因；路线上这条一直没勾。
- ✅ 打印件验真二维码落地（**ADR-0015**）：占位框替换为真码。设计正是此前登记的顾虑
  的答案——「按单据号开放查询」违反 §8，故走**签名不透明令牌**（HMAC 域分隔派生密钥，
  sm 套件下 HMAC-SM3；不可枚举、不可伪造），公开核验端点 `GET /api/print/verify`
  只回显纸面已印字段 + 现势状态（同意已撤回会报告），限速 30 次/分/IP；密钥轮换走
  `verification_keys` previous 宽限（纸比密钥长寿）。二维码生成上移平台侧 `qrsvg.py`，
  spd 经 `platform.py` 委托回来（单向依赖方向修正）。扫码落 `/verify` 静态页，令牌在
  `#` 片段不进访问日志。回归 `test_print_verify.py` 15 条，五处变异各自转红。
  遗留（接受，ADR 记档）：无打印留痕/单张吊销——要补在方案 C 之上加打印流水表即可。
- ✅ `routers/monitor.py:79` `"success"` → `"succeeded"`（带回归测试 `test_monitor_overview.py`）。
- ✅ `spd/reporting._score` 补上机构与周期过滤。原先渲染器签名收了 `org_id` 与 `period` 却**两个都没用上**——任何机构、任何周期的报告，这一段都是同一份「最近 20 条」：甲机构的报告里印着乙机构的排名，一季度的报告里印着二季度的分数。此前登记为「需业务决策」是过度谨慎了：`period` 过滤毫无歧义；`org_id` 按 `object_type` 分派（机构/团队/村医/医师四类各自查归属）也是唯一自洽的读法，口径取「恰好属于该机构」——与本模块其余段落一致（`_screening` 等都是 `X.org_id == org_id`），报表段落之间口径不一样比少一段更难查。机构名下没有考核对象时返回空表，**不退回全域数据**（那是别家的数字）。回归四条，变异验证：退回原实现四条全红。
- ✅ `scheduler.py` `_release_lock` 校验持有者，防误删他实例锁（token 所有权 + Lua 比对删，回归测试 `test_scheduler_lock.py`）。
- ✅ 调度双跑收窄：① 锁续期心跳（每 TTL/4 秒「仍持有才续期」，`_RENEW_LUA` 原子比对+expire；实例崩溃心跳停、锁 ≤TTL 自愈；易主即停并告警；续期失败 5s 快速重试）——超 TTL 长任务不再丢锁；② `tick()` 拿锁后**重新确认到期**——锁只保证不重叠、保证不了不重复，陈旧到期快照会让已被他实例跑过的任务再跑一遍。回归 `test_scheduler_lock.py` 四条。
- ✅ 手工触发 `routers/jobs.py` 的 `run_job` 改走执行锁（口径：**加锁校验**）。抽 `scheduler.job_lock` 给调度循环与手工触发共用，占用中返回 409。**两层锁**：Redis 锁只挡跨实例，同进程内调度线程与请求线程照样重叠，且不配 Redis 是默认形态（那时锁等于不存在）——故补进程内 `threading.Lock`。回归四条，两处变异验证。
- ✅ CI 加了 Redis service，新增 `test_scheduler_lock_real_redis.py` 七条在**真 Redis** 上验：`SET NX` 互斥、TTL 确实设上、两段 Lua（比对后删 / 比对后续期）的原子语义、锁到期可被接管、`job_lock` 完整路径（拿锁→心跳顶 TTL→退出释放）。假 redis 的 `eval` 是自己写的 if，证明不了脚本本身对——两处变异（Lua 退化成无条件 del / 无条件 expire）各自转红。
- ✅ 迁移-模型**列级** parity 门（`test_迁移与模型的列集合零漂移`）：真 PG 上跑完 `upgrade heads` 后逐表比对 inspector 与 `Base.metadata` 的列集合，两个方向都查（模型有迁移没建 → 生产 UndefinedColumn；迁移建了模型没有 → 多半是删列忘写迁移）。ADR-0002 停用 `create_all` 后这是最后一处「开发 SQLite 看着正常、上线才炸」的口子。已变异验证：给模型加一列不写迁移，当场转红。**补充（2026-08-22）**：那条只跑在 integration（本地默认 skip），等于把「改模型」这个纯本地动作的反馈推到 CI，与 CLAUDE.md §7「别把 CI 当第一道防线」相悖。已补 `test_migration_model_parity.py`：同一份比对逻辑（抽到 `tests/schema_parity.py`，不是第二套实现）跑在一次性 SQLite 空库上，约 7 秒，进 test-unit。两条各守一半——SQLite 那条证明「列写全了」，PG 那条证明「PG 认这个类型」。
- ✅ 机构树体检增报**层级错位**：`tree-health` 新增 `broken_chains`（转诊阶梯上父子层级不相邻的机构，带期望层级与自根到叶链路）与 `max_depth`（信息项）；`referral_ready` 收紧为"无孤儿且无错位"。判据是层级相邻而非链路长度：市级四层合法不误报、county/city 之上的挂载（县疾控、市协作院）不参与判定，而 county→村室→村室 三层照报。回归 `test_org_tree_health.py` 七条，改函数级 client 消除顺序耦合。
- ✅ 加固 `test_monitor_overview.py`：每条用例先清空 `job_runs` 再播种，seeded 行不会被别的失败记录挤出 limit-5 窗口（断言改精确相等）；另补一条钉住"只返回最近 5 条、按 id 倒序"的窗口语义。已验证单跑/乱序均绿，且把端点 limit 改小会让新用例转红。

### 工具链
- ✅ 清掉 8 处存量 lint（5 未用 import + 2 无占位 f-string + 1 未用变量改显式 assert），`make lint` 归零；`make verify` 的 typecheck 步改为渐进式 warning 不阻断——verify 现可用。
- ✅ mypy 存量 **187 → 0**（CI 口径，全程零行为改动）。分三步：①抽 `deps.row_dict()` 收掉最大一族（`dict(query.all())` 重复 35 处，Row 与 dict 的类型对不上）；②抽 `concurrency.ensure_present()` 收掉「insert_if_absent 后重查」那族（8 处，正常路径必不为 None，但那是**推理**不是保证——并发删除会变成 500，现在给 409 与人话）；③其余逐个处理：分支复用变量改名（workflows/followup/assess/drgs）、可空外键当字典键改宽键声明、混值字典就地标注、`Result.rowcount` 用 `cast(CursorResult, ...)`。
- ✅ **lint 转阻断**（CI 实测 ruff 0 项）；`requirements-dev.txt` 给 ruff/mypy 钉上界——本仓库无 lockfile，门一旦阻断就必须可复现（实测 mypy 2.3 报 139 处、1.19 在同一份代码上报 187 处，版本飘一下结论就变）。
- ✅ **mypy 环境探针 `scripts/check_mypy_env.py`（阻断）**：`ignore_missing_imports=true` 的代价是——mypy 解析不到的库会被**静默当成 Any**，依赖它的代码全部「通过」。本轮真踩到：开发机的 `mypy` 是 `uv tool install` 的隔离环境（没有 SQLAlchemy），同一份代码本地报 **41** 处、CI 报 **187** 处，差的 146 处全是 ORM 相关。探针先 `reveal_type` 探 sqlalchemy/pydantic，是 Any 就直接失败并给修复指引，杜绝再拿假绿下结论。
- ✅ **typecheck 已转阻断**：`make verify` 去掉 `-` 前缀、CI 去掉 `|| echo ::warning`。六项能力（build/lint/typecheck/unit/integration/smoke）自此全部阻断。
- ✅ **CI 解释器与生产对齐**：两个 job 改用 `PYTHON_VERSION: "3.12"`（与两个 Dockerfile、ruff `target-version`、mypy `python_version` 同版）。切换前在 3.12 上实测过全套：compileall / alembic upgrade heads（247 表）/ ruff 0 项 / mypy 139（与 3.11 逐条相同）/ 单元 1465 passed / smoke 2 passed / app 起得来。新增 `test_python_version_alignment.py` 两条把四处钉在一起（版本一致 + 不许写死版本号，后者扫全部 workflow 且带不带引号都认）——以后升级要么四处一起改、要么用例变红（此前它们没有任何互相约束，正是漂开的原因）。

### 让 CI 变真（关联 ADR-0002）
- ✅ 覆盖率门禁转阻断（落地实测 87%，门槛 70%）；集成/迁移门在真 PG 上转阻断。
- ✅ 生产禁用 `create_all`（lifespan 环境分支 + 守卫测试 `test_adr0002_create_all_guard.py`）、`start.sh` 启动前 `alembic upgrade heads`、README 修正复数（ADR-0002 已 Accepted）。

## Next（治理逐块推进，只进不退）

- ✅ **11 处被顶下去的 docstring 换回首行（2026-09-02，P2-4 的 docstring 半边）**。
  形状全部一样：补横向越权校验那轮把 `assert_org_writable(...)` 插到了 docstring 上面，
  Python 不报错但 `__doc__` 为 None，接口文档页上这 11 个端点是空白。守卫
  `test_docstring_position.py`：源码形状（docstring 不得是第二条语句）+ 生成物闭环
  （有 docstring 的端点 OpenAPI 必有 description，分母从源路由模块推导——`app.routes`
  上挂的是 `_IncludedRouter` 包装，APIRoute 只剩个位数，拿它当分母会什么也守不住）。
- ✅ **月度期间口径收敛到单一真源（2026-09-02）：P1-34 五处 `2026-13` 放行清零**。
  财务记账、薪酬、基金周期预结的 `period` 与月报导出、质控统计的查询参数原先各自写着
  "四位-两位"的正则——只管形状不管日历，`2026-13` 入了库就是一条永远对不上任何月份的
  记账，进了过滤就是一份全空却不报错的报表（与 D-3 假日期同一个坑的月度版）。`datetypes`
  新增 `PeriodStr`（形状 + 真实日历），查询参数配 `deps.require_month`（原 422 文案不变、
  月份不存在给新文案），`period_bounds` 的形状改为引用真源对象。守卫与 DateStr 那条同构：
  扫 `app/` 全部字符串字面量，别处再写月度正则即红；两处变异转红。
- ✅ **PII 出口脱敏守卫落地（2026-09-02）：P1-33 从"靠人记得"变成棘轮，首个洞已堵**。
  `privacy.py` 写着"新增返回身份证号/电话的接口必须复用本模块"，此前没有任何检查在证明它。
  新守卫 `test_privacy_egress_guard.py` 的分母从路由对象推导（946 个端点的响应模型递归
  展开找 `id_card`/`phone` 字段），判定沿调用链找 `desensitize`/`mask_*`（正确写法多半在
  `_out()` 帮手里，只看函数体会满屏假红），`/api/portal/` 按口径视为居民本人侧，其余必须
  逐条书面登记且不得腐烂；裸 `dict/Any` 契约这个唯一盲区逐个复核登记而非给总数。盘出的
  第一个洞：`POST /api/patients` 幂等命中既有档案时把别人录入的那份原样返回——一个只知道
  证件号的账号借"建档"就能套出电话，现改走 `desensitize`。登记的 24 处明文出口
  （慢专病工作台行内电话 17、120 呼救人回拨 4、村医通讯录 3）是**待产品裁定**（P1-39）
  而非放行：电话随访默认人工拨号、120 靠回拨，一刀切掩码会把业务做没。三处变异各自转红。
- ✅ **读-改-写真追加清零（2026-09-02）：P1-28 八处逐处落地，P1-31 陈账销账**。
  `obj.col = f(obj.col, 新值)` 把旧值读进 Python、算完整体写回，并发下后写的把先写
  的盖掉，而两笔的日志看上去都成功了。按列的类型三种修法：**字符串追加**（孕产妇风险
  因素 ×2、高危儿风险备注）改 `concurrency.append_text`——拼接下沉到 SQL 由行锁排队，
  空则不带分隔符；**处方药师审核**改成一条带 `status='pending_review'` 条件的 UPDATE，
  状态迁移与意见追加同条 SQL，顺带关掉"两位药师同时审、后者覆盖前者结论"的双审竞态
  （真 PG 八路并发恰一路审到，其余七路拿 409）；**JSON 列整体覆写**（复诊日志、外呼
  结果+证据、召回联系记录）没有可移植的原子追加，进 `serialized_on` 行锁临界区、
  先 `db.refresh` 再追加——该闸门原是 billing 私有的，上提到 `app/concurrency.py`。
  防复发规则同步升级：豁免只认"临界区内**且 refresh 之后**"（顺序写反照样红，合成
  代码五形状自证）；欠账清单从"函数名"改成"**函数 × 条数**"并加"条数必须相等"的
  防腐烂用例——只登函数名时，`record_call_result` 名下两条真追加与两条幂等回填混在
  一起，修掉真追加后再塞一条新的照样报绿；这条新用例顺手揪出三条早已修好却还挂着
  的陈账（`refund_payment` / `_finish_task` / `dispatch_edu_push`，后者即 P1-31）。
  回归 11 条（顺序语义逐字不变 + 静态钉 + 不得回清单）+ 真 PG 三条八路并发直测，
  三处变异各自转红。
- ✅ **静默双写清零（2026-09-01）：P1-29 五条逐条落地**。三条真不变式由迁移
  `b8e3d5f70a91` 下沉为**部分唯一索引**（在院唯一 / 号源唯一 / 首次病程唯一），
  接口改走 `insert_or_conflict`——此前它们都是"先查再建"的 check-then-act，
  并发下不报错、**静默写出两条**：同一个患者被登记进两张床、同一时段放出两份号、
  同一次住院两份法定文书。号源那条特意拆成两条部分索引：SQL 里 NULL != NULL，
  不挂医师的检查/检验号源在单一复合唯一索引下等于不设防。
  另两条如实处置而非硬修：`create_settlement` 是**陈账**（早已由 e5b7c9d1f3a4
  修好，清单没跟上）；`bill_details` 的**判据被推翻**——床位费/护理费按天逐条记
  同一收费项是正常业务，加唯一约束会拒掉第二天的计费，已移出清单并用特征化网
  钉住现状（含"谁按旧判据加约束就变红"）。回归 12 条（含**绕开接口层直插证明库里
  真拦得住**——索引"在不在"与"拦不拦得住"是两回事），三处变异转红。
- ✅ **迁移-模型结构漂移清零（2026-08-31）：P1-27 两个方言档 39/41 → 0**。迁移
  `e7c4b19d02fa` 一次收口三类"模型说了、库没做"的差异：补 **14 处外键**（此前生产
  PG 根本不做参照完整性校验，孤儿行静默落库）、收紧 **25 列 NOT NULL**（迁移建成
  可空、模型 NOT NULL，非 ORM 写入能把 NULL 塞进 created_at 与 JSON 列）、删掉
  **2 处**与模型唯一索引重复的无名唯一约束（删前先确认同列另有唯一索引在守，
  不会出现"删完没人守唯一"）。存量冲突按 CLAUDE.md §4 走不阻塞路径：**逐项**探测、
  探到孤儿行/NULL 只跳过该项并打**指名主键**的 ERROR 日志，人工处置 SQL 写进迁移
  docstring；纯 DDL，不 UPDATE/DELETE 任何存量业务数据。跳过分支由
  `test_migration_conflict_skip.py` 六条**行为**回归咬住（三处变异转红——其中"日志
  指名"那条最初假绿：冲突行 id=1 时退化文案"共 1 条"里也有个 1，改用可辨识主键后
  才真的咬住）。
- 🔧 **本机集成档不再赊给 CI**：新增 `server/scripts/dev_services.sh`（start/env/stop），
  一行拉起本机 PG+Redis 并导出两个 `*_TEST_URL`。开发容器里这两个服务往往**已经装好
  只是没跑**，而缺服务时 integration 档会**整档 skip 且退出码为 0**（"没跑"与"全对"
  的绿灯长得一样）。落地即验：`make test-integration` 本地 **19 passed / 0 skipped**
  （真 PG 12 + 真 Redis 7），本轮 PG 档漂移清零正是这样实测的，而不是只跑 SQLite
  就宣布结论。

- ✅ **契约治理收官（2026-08-31）：收官两批 40 → 0，946 端点覆盖率 100%，欠账账户销户**。
  收官批②八散点（patients/vaccination/eldercare/exams/publichealth/consultations/
  appointments/emergency，21 端点，40→19）+ 收官批①（chronic/insurance/integration/
  medication，19 端点，19→0），**四十六模块全数 FULLY_GOVERNED，基线归 0 从此只许为 0**。
  取证同前法（capture_plugin 套件级逐字节、噪声底先归零：831/91 键 0 差异；变异 5+5 转红；
  patients 授权四端点带脱敏双视角与 AccessLog 留痕逐字节钉）。六轮并行批合计 450→0。
  收官轮同日另收六件：**created_at 棘轮 2→0 收官**（blood_stocks 顺路补齐；admissions
  冻结表先过 ADR-0018——历史行回填 1970 哨兵而非抄 admitted_at，取舍见 ADR）；
  **P2-31 渲染器监听窗口全仓根修收官**（acorn AST 清点 8 文件 22 条挂载移到 await 前，
  守卫升级 ROOT_FIXED_RENDERERS 清单式 12 渲染器 + 5 处登记例外）；**P1-18/P1-19
  事务边界专档**（8 条资金/库存/状态机链路「失败无半写 + 重来恰一次」，真 PG 档扩至
  11 条）；顺带实证并随即修复 **spd 并发认领洞**（claim/批量/办结三处条件 UPDATE 收口，
  回归 7 条 + 防拆卸静态钉）；**P1-20 测试 fixture 去重**（136 文件收敛 conftest 共享
  三件套，净 −1914 行，两轮受控全量探针验证零回退；顺带根修两处 CWD 相对路径假红）；
  **P2-26④ 生产锁剔除测试工具**（运行时依赖 13→11、lock 重生成，镜像不再含 pytest，
  ADR-0017 后记留档）。
- ✅ **多 agent 并行批第三、四轮（2026-08-31）：二十四模块 178 端点，基线 218 → 40，覆盖率 95.8%**。
  七个并行会话分两波（billing+inpatient 29 / admin_mgmt+maternal 32 / 四小簇 32 //
  药事三簇 25 / 临床五小簇 30 / 运维八小簇 30），主会话分六次收基线
  218→189→157→125→100→70→40，每批隔离 worktree 验证后推送。取证升级点：Money 的
  int/float 一律 `type(x) is int` 显式钉（dict 相等对 15==15.0 是盲的）；cost 汇总同一行
  两族并存（0.0 桶起加恒 float vs .get(id,0) 兜底 int|float）——判据是产地不是字段名；
  todos 分节真多态照 drilldown 先例宽字典+type 自描述；两个真下载端点（audit/export
  NDJSON、附件字节流）照 CsvResponse 先例走媒体类型声明并入册豁免清单钉（+2）。
  四轮合计 **410 端点（450→40）、三十四模块进 FULLY_GOVERNED**；剩余 40 处零散分布在
  exams(3)/insurance(5)/integration(5)/chronic(5)/consultations(2)/medication(4)/
  patients(4)/publichealth(3)/vaccination(2)/eldercare(2)/emergency(1)/appointments(1)
  等部分治理模块，可按同法一批收完。
  同日另收：**P1-9 启动种子化加固**（逐块隔离 + PG advisory lock 专用连接串行化 +
  撞键按已种好继续，见 TECH_DEBT 销账行与 ADR 先例注释）；**CI 两条 e2e flake 根修**
  （原生表单提交竞态——监听先于 await + shared.js document 层兜底，P2-31 登记剩余面；
  瞬态断言改稳定终态）；手写 SQL 棘轮 15→17（advisory lock 为 ORM 无法表达的合法增量）。
- ✅ **多 agent 并行批第二轮（2026-08-28）：六模块 118 端点，基线 336 → 218，覆盖率首破 77%**。
  同一套编队与纪律（三会话各领一簇、互不碰文件、主会话分三次收基线 336→302→259→218、
  每批隔离 worktree 验证后推送）：①esb 13 + education 21（payload/steps 外来 JSON 宽 dict
  透传且用自定义键钉"不许滤键"、Float 列 score→float 与 Money 相反）；②spd/followup 29 +
  spd/referral 14（auto-match 两分支二选一联合、详情独有 steps 继承拆两模型防注入 null、
  分级审核语义零改动）；③spd/population 28 + vaccine_supply 13（患者 brief 条件键双向钉、
  closed 宽字典、360 档案与纳管详情的 paths 两个形状分建）。三批取证 234/712/838 个组合
  目标范围全部 0 差异，合计 15 处变异验证转红，零跳过。累计十三模块进 FULLY_GOVERNED，
  欠账 218/946——首次降到总端点四分之一以下。
  同日另收：**e2e 去 flaky**（居民自查用例改断稳定终态"待受理"——"风险等级"是瞬态文本，
  断言它是在跟自家重画抢时间，CI 慢半拍就输；含冷启动时序连跑全绿）。
- ✅ **多 agent 并行批（2026-08-28）：七模块 114 端点，基线 450 → 336**。三个并行会话各领一簇、
  互不碰文件，主会话统一收基线与 `FULLY_GOVERNED`（分三次下调 450→418→375→336，每批一个
  自洽提交、隔离 worktree 验证后推送）：①质控三簇 quality 20 + dataquality 6 + rules 6
  （条件键 exclude_unset、qc-summary 分组键 int|str、rules sample 三型并存 bool 不得变 0/1）；
  ②spd/assess 24 + spd/tasks 19（scores-analysis 两分支键序不同→二选一联合、动作回执 27 键
  与清单行 29 键拆两模型）；③spd/care 31 + spd/workbench 8（Float 列→float 与 Money 相反、
  workbench/team 三角色条件键三种键序各钉一遍、4 个重名模型改名防 OpenAPI 长限定名改写）。
  取证均为 capture_plugin 套件级前后逐字节比对（60/105/691 个组合各 0 差异），三批合计
  12 处变异验证转红。零跳过——没有一个端点靠"跳过"绕行。
  同批并行完成：**P1-1 居民端调阅留痕**（28 读端点接 AccessLog，见 TECH_DEBT 销账行）、
  **E2E 复活接入 CI**（11/11 零 flaky 阻断 + 自证闸门，两条假绿修真，揪出 m.js loadSpd
  并发渲染竞态真 bug——已建任务卡未夹带）、**技术债账本 14 条实证销账 + README 真数**。
- ◐ 接口契约棘轮：按 `docs/接口标准与治理.md` 逐块迁移。已治理 12 模块，基线 **757→741**。本轮做掉 encounters 的 `/archive/360`——全平台聚合度最高的接口（一次吐出一个人的就诊/检查/慢病/处方/结算/体检），嵌套九段**逐段建模**而不是 `dict[str, Any]`（写成 Any 等于没声明契约，而这个接口恰恰最需要）。先补特征化网 `test_archive_360_contract.py` 十三条钉住键集合与类型、再加契约、加完网照样绿（响应字节不变）；网里六段都造了数据——空列表什么都钉不住。两处变异（契约少一个字段 / 字段名写错）各自转红。本轮再做掉 performance 的 `/orgs` 机构计分卡，基线 **741 → 740**，`performance` 三个端点全部有契约、已进 `FULLY_GOVERNED`。两处麻烦点：`weights` 的键来自指标表是**动态的**（只能 `dict[str, float]`）；`detail` 五段**混形状**（三段分子/分母、两段裸计数），逐段建模而非 `dict[str, Any]`。`score` 特意验过恒为 float——`_normalized_weights` 表空时退回非空默认，求和恒在浮点上做；若可能是 int，声明 float 就会把 `0` 变 `0.0`，那是改字节不是治理。特征化网 `test_performance_orgs_contract.py` 十条（造了五个维度的真实数据，全零响应什么都钉不住），并逐字钉住折算值（42.5 / 32.5 / 67.5）；三组参数下**响应字节逐字节比对一致**；两处变异各自转红。✅ **`metrics` 与 `analytics` 两簇已做完**，基线 **724 → 719 → 709**，两模块均进 `FULLY_GOVERNED`。刻意**只做契约不动口径**（这两簇正是 ADR-0007 口径合并的交集区，混着改就分不清「这是治理」还是「这是改数」）。本批最要紧的建模判断：`round()` 与 Money 列派生的数值一律 `int | float` 而非 float——实测同一个 `total_amount` 字段一行返回 `1234.5`、另一行返回 `100`（int），`Money` 是 `Numeric(14,2, asdecimal=False)`，整数值读回来就是 int；写成 float 会把 `100` 变 `100.0`。变异验证直接抓到了这一字节差，不是理论推演。两处**多态** items（`drilldown` 八种行形状、`performance-report` 失败项多一个 `error` 键）用宽字典而非逐字段——后者会给成功项注入 `"error": null`，同样改字节；多态不等于无契约：`drilldown` 同一响应里的 `fields` 就是行的字段清单，有用例钉住两者相等。metrics 46 个请求 + analytics 17 个请求，加契约前后逐字节一致。✅ **`reports` 三端点也做完，基线 709 → 706**，并顺带**把棘轮的判据修对了**：两个 CSV 导出直接返回 `StreamingResponse`，`response_model` 对它们没有意义（函数不返回可序列化对象，FastAPI 也会跳过模型）——把这类端点永远算作欠账，等于往棘轮里掺一笔**永远还不掉的账**，数字就不再表示「还有多少接口没契约」。判据放宽为「有 `response_model` **或**显式声明了非 JSON 媒体类型」，且**从路由对象推导、不是手工豁免清单**。放宽**没有白送任何端点**（先查过：全仓库只有 `printing` 的 12 个端点声明过 `response_class`，而它们本来就都有 `response_model`），并有用例钉住「靠媒体类型脱账」的清单，拿空 `responses` 刷低欠账会让它变长。写法见 `docs/接口标准与治理.md` 新增的对照表——`printing` 的 `response_model=str` 在 CSV 上不适用，改用自带 `media_type` 的 `CsvResponse`，**声明与实际返回是同一个类**。
- ◐ **`portal` 开工：先做 auth 组 + 两个公开列表（10 个），基线 706 → 696**，portal 余 48。**刻意没做完整个 portal**：`my-archive`(2) 与 `surveys`(1) 三个遗留端点与 `me/archive` 共用 `_build_archive`，拆开做会把同一个形状建模两次，故留给 `me`(19) 那批一起做；`spd`(26) 再一批。本批关键是两个**条件键**——`auth/sms/code` 的 `debug_code` 与 `auth/wechat/authorize` 的 `mock_code`：声明成带默认值的可选字段会给**每一个**响应注入 `null`，既改字节，又等于在生产响应与 OpenAPI 里公告该字段存在（`debug_code` 是登录验证码的回显口子，P0 整改专门收紧过它——契约不该把刚收紧的东西又摆回台面）。两个端点用 `response_model_exclude_unset=True`，**两条分支都逐字节比对过**：回显开时有该键、关时整个键不出现。另有 `price-list.price` 的 Money 陷阱（整数价仍是 int，否则公示页「50 元」变「50.0 元」）。
- ✅ **`portal/me` 组 + 三个遗留端点做完（22 个），基线 696 → 674**，`app/routers/portal.py` 契约欠账清零、已进 `FULLY_GOVERNED`。三处判断：①`me/family` 的**条件键** `member_id`——本人那一行没有它（本人不是一条代管关系），声明成可选字段会注入 `"member_id": null`，客户端照着 null 去调 `DELETE /me/family/None` 就是平白多出来的错误路径，故用 `response_model_exclude_unset=True`；②Money 陷阱这一批出现了**四处**（账单三项、费用明细单价与金额、分类汇总的**值**、押金余额），整数金额声明成 float 会把「200 元」变「200.0 元」；③`_build_archive` 被三个端点共用（`me/archive` 与两个已废弃的 `my-archive`），只建一个 `ArchiveOut`，并有用例钉住三者形状相等——否则日后改一处漏两处。消息两端点**复用** notifications 已有的 `NotificationOut`/`UnreadCountOut`，不另建同形模型。29 个请求加契约前后逐字节一致，四处变异各自转红（见 `test_portal_me_contract.py`）。
- ✅ **顺带把棘轮的模块 key 按包限定**（spd 的加 `spd/` 前缀）。此前 `app/routers/portal.py` 与 `app/spd/routers/portal.py` 被合并成一个 key `portal`——后果是前者治理干净了也**进不了** `FULLY_GOVERNED`（合并后的 key 还带着后者的 26 项欠账），于是它被改回裸 dict 时不会单独变红，只剩总基线兜底，而总基线是可以被别处的治理抵消的。这正是该文件开头声称已关掉的那种「静默回退」，只是换了个入口，本批治理完 portal 才让它现形。补了反空转守卫 `test_两个包里的同名模块不被合并成一个key`：去掉前缀当场转红。
- ✅ **顺手修**：`/me/deposits` 的 `amount`/`balance` 声明成了 `float`（a911f61 引入），而 `Deposit.amount` 是 Money 列、`deposit_balance()` 也返回 int——1000 元的押金以 `1000.0` 出账。与本批同一类陷阱，就近修掉并补回归。该端点目前无前端消费方，改回 int 不影响任何页面。
- ✅ **`spd/portal` 26 个端点做完，基线 674 → 648**，两侧居民端契约就此都清零、都进 `FULLY_GOVERNED`。最要紧的是 `/screenings` 的**三种形状**——草稿+量表（带 `answered`/`total_items`）、草稿无量表（只有四个键）、落库（带 `id`/`result`/`can_apply`），逐字段建模会把三者的字段互相注入 null，故 `response_model_exclude_unset=True`，三条分支各钉一遍。`score` 是 `int | float`：有量表时 `round(total, 2)` 是 float，无量表时兜底字面量 `0` 是 int。与平台侧的 Money 陷阱**方向相反**，spd 这边多是 Float 列（`measurement.value`、`assessment.score`），整数值读回来就是 `140.0`，声明 float 才是原样。两处我自己犯的错都被机制当场抓到并写进了模型 docstring：转诊详情模型继承列表模型、凭空要求了一个详情不返回的 `created_at`（响应校验拦下）；`SpdScreeningOut` 字段顺序把 `advice` 排在 `result` 前面（序列化按声明顺序走，逐字节比对拦下）。41 个请求加契约前后逐字节一致，五处变异各自转红（见 `test_spd_portal_contract.py`）。
- ✅ **`spd/config` 58 个端点做完，基线 648 → 588**，已进 `FULLY_GOVERNED`。config 是个包（ADR-0008 拆的），6 个子模块，**分三批三个提交**做——一次比 58 个端点，逐字节比对出了问题不好定位，粒度本身就是这套办法的价值。①catalog 9 + centers 4（648→635）：`ProgramDetailOut` 继承 `ProgramOut` 是对的（详情是列表的**严格超集**——与 spd/portal 那批的转诊详情正相反，那个不是超集，同样写继承就错了）；`target_low`/`target_high` 可空 Float；`org-tree` 自引用递归。②paths 7 + devices 9（635→619→614）：`_template_out` 三形状用 `exclude_unset` 且 `nodes` 须在 `node_count` 之前；**判据第二次放宽**——204 无响应体也算声明了契约，这次**确实白送了 5 个端点**（上次放宽媒体类型是 0 个），故账要拆开看：真治理 16 个 + 放宽降 5。③scales 15 + teams 12（614→588）：服务包 `price` 是 Money 列；标签新建与列表**不同形**（列表没有 active）；两个二维码改用 `_base.SvgResponse`。三批合计 102 个请求逐字节一致，十五处变异各自转红。
- ✅ **顺手修**：村医绑定二维码编的是 `/m/doctor.html`，而那个地址**没有路由**（`main.py` 只挂了 `/static`，医生端入口是显式的 `/m/doctor`）——印出去的码扫开是一张 404，村医绑不上账号还看不出是码的问题。回归用例不比对字符串（写错成另一个不存在的路径照样绿），改成把 path 抠出来真请求一次。
- **两条「不由字节决定」的记录**（免得日后被当成守卫失效）：`last_sync_at` 从 `str` 改 `str | None` 零处转红（handler 永远给值）；去掉 `response_class=SvgResponse` 也不改响应字节（返回的类自带 media_type），变的是 OpenAPI 会回落成 `application/json`——规格书写了假话。这类判断的理由是「契约要诚实」，不是「字节会变」，不咬人的变异不能当证据。
- ✅ **一次十二个模块，基线 588 → 470**（118 个端点）：medwaste 11 / clinical_docs 9 / materials 9 / surgery 10 / accounting 9 / disease_programs 9 / rbac 9 / surveillance 9 / tcm_heritage 9 / workflows 9 / outpatient_docs 13 / fund 12，十二个模块全部清零。覆盖率首次过半（**50.26%**），已治理模块 24 → 36 个。
- 🔧 **取证方式换了**，这是本轮真正的产出：不再每个模块手写一份捕获脚本，改成给 app 装一个中间件，把**整个测试套件**跑出来的每个响应按 `(方法, 路由模板, 状态码)` 记下字节，加契约前后各跑一次逐项比对——一次覆盖 **1847 个组合**，治理十个模块和治理一个模块的取证成本一样。工具 `tests/capture_plugin.py`，用法与**三条注意事项**写进了 `docs/接口标准与治理.md`：①用之前必须先量噪声底（同一份代码跑两次差异必须为 0，实测第一次有 2 处随机项）；②它覆盖不到测试没跑过的端点，那是**沉默的缺口**不是保证；③改了路由代码就要重跑（pytest 启动时导入 app，之后的编辑不影响正在跑的那轮——踩过一次）。
- 本轮的建模判断：Money 列一律 `int | float`（accounting 全模块、fund 全模块、materials 采购与耗材单价——fund 那个直接决定各机构分到多少钱，不是显示问题）；条件键用 `exclude_unset`（accounting 凭证的 entries、rbac 内置角色的 note、fund 超额预付的 warning、**tcm_heritage 决策点的 answer/explain**——最后这个是嵌套条件键，学员拉题目时答案整个键不出现，声明成可选字段等于把答案的存在公告出去）；「新建回执」与「列表行」键集合不同的一律两个模型，不硬套继承。
- ✅ **ADR-0006 收官批的 20 个端点已补契约，基线 470 → 450**（覆盖率 52.38%）。`cssd` 加回 `FULLY_GOVERNED`（搬家那个提交里因搬入 3 个无契约端点短暂移出——**总欠账当时一点没变**，470→470 只是换了名下，只有总基线的话那次回退会完全静默）；`surveys`/`triage` 生而全契约。两处会改字节的判断：`ExamResource.price` 是 Money 列（捕获里实测到 `"price":240`，声明 float 就变 `240.0`）；`survey_stats` 的**字段顺序**照 handler 实际出键排——它 `pop("count")` 后又重新赋值，`count` 因此被挪到 `distribution` 与 `negative` 之后。套件级捕获 1856 个组合前后比对，**落在这 20 个端点内的差异 0 处**；捕获盖不到的两处另补了用例（`GET /api/surveys` 一次没被跑过、`GET /api/cssd/requests` 只跑到过空列表——空集钉不住字段），四处变异各自转红。
- 剩余候选：`spd/care`(31) / `spd/followup`(29) / `spd/population`(28) / `spd/assess`(24) / `education`(21) / `quality`(20) / `admin_mgmt`(20) / `spd/tasks`(19)…（共 450，模块数 50）。
- ✅ **搬家带来的 8 个端点契约已补齐**，基线 **740 → 732**，`cssd` 与 `performance` 双双回归 `FULLY_GOVERNED`。最容易踩的是 `cssd` 的 `total_cost`：它是 `round(totals.get(id, 0), 2)`，**没有成本项时返回 int `0` 而不是 `0.0`**，声明成 `float` 就会把 `0` 变成 `0.0`（改字节）。契约写 `int | float` 原样透传，变异验证：改成 `float` 立刻转红。八个端点响应字节逐字节比对一致（含空集与有数据两条分支）。
- ✅ `created_at` 欠账迁移**收官**：52 → **2**（16 个迁移批次，平台链+spd 链，全部常量默认回填→batch 撤默认范式，全程响应字节不变）。仅剩有意留置 2 张：`blood_stocks`（小型 upsert 表，价值低降级）、`admissions`（核心表，改列需先 ADR）。此后新表一律带 created_at（棘轮基线=2 顶住）。
- ✅ 测试隔离修复：**七条**用例在 `pytest -k` 子集下会红（整模块跑得过，纯靠执行顺序）。根因不是共享库本身，是**跨用例借数据**——`GET …?keyword=张三` 取上一条用例建的人、`enrollments[0]` 取上一条用例建的档案、DRG 统计里指望上一条用例的病例已在。改法：把前置数据交给**幂等的模块级 fixture** 负责交付（已有就返回、没有才建），用例声明依赖即可。`path_template` fixture 顺带把模板发布掉——「可用的模板」本就该由建它的 fixture 交付，而不是靠 `test_publish_*` 排在前面顺带做掉。现每条单选跑均绿。
- ✅ **测试隔离全量普查（2026-08-22）**：112 个测试模块**逐个单跑**，零失败——"单跑就红"这一类已经干净。
  再换一个方向查"顺序依赖"：全量套件按模块名**逆序**跑一遍（不引入 pytest-randomly，第 12 条不为这个加依赖），
  抓到**一条**真的顺序依赖，而且是条鉴权守卫——`test_authz_matrix.py::test_未守卫写接口清单必须是审阅过的白名单`。
  根因：pytest 在跑任何用例前会**导入全部测试模块**，`tests/test_p0_fixes.py` 导入时就往共享的 `app` 上挂了
  `POST /api/_test/boom`（`include_in_schema=False`，用来触发 500）。正序时 test_authz… 先导入、看不到它；
  逆序时看得到，于是这个测试脚手架被当成"没配守卫的写接口"。
  改法不是无条件跳过 `include_in_schema=False`——那等于给隐藏写接口开免检门，而这是**鉴权**守卫，静默跳过即漏洞。
  改成"跳过 + 清账"：跳过的每一条都必须在 `TEST_ONLY_ROUTES` 里，新增任何隐藏写接口当场转红（已变异验证）。
  另加 `test_矩阵不受测试脚手架路由影响_与模块导入顺序无关`：强制先导入那个模块再重跑收集，
  让这个回归在**正序**下也能复现——否则它只在"逆序跑全量"时才看得见，而没人会天天那么跑。
- ◐ 三套并行子域（ADR-0003 已 **Accepted**）：转诊读侧聚合的**接口侧**已落地——`GET /api/portal/me/referrals/all` 把平台 `referrals` 与 `spd_referral_cases` 并成一份（带 `source` + 分源中文标签 + 完整时间戳排序）；两个老接口响应字节不动（两条特征化用例各钉一个）；注册制登记源，子系统关掉自动降级为平台单源。实施中确认**无需去重**（两批单子真正不相交，spd 从不写 `referrals`），真正要处理的是**同名不同义**的状态码（平台 accepted=已接收 vs spd accepted=县级医院已接收），故每条带 `source` + 分源标签，措辞与 `m.js` 既有文案逐字对齐。
- ✅ **居民端已切到聚合接口**：`static/m/m.js` 两个页面均取自 `/me/referrals/all`，慢专病页用 **`?source=spd` 服务端收窄**（不能客户端 filter——条数上限是合并后才截的，平台转诊一多就会把慢专病的单子整段挤出窗口，页面显示「暂无」而其实有在办的；回归用例已钉住）。**用户可感知的转诊孤岛就此消除**。顺带删掉前端两张 `REFERRAL_STATUS`/`SPD_REF_TEXT` 标签表——同一份映射不该有两个副本，状态文案权威统一到后端 `status_label`；两个页面的卡片渲染合并为一份 `referralCard()`；详情链接直接用后端 `detail_path`（已带 patient_id，代管家属才点得开）。静态守卫 `test_portal_referral_frontend.py` 七条防复开，四处变异验证。
- ✅ 业务端转诊文案收归后端：`ReferralOut` 新增 `status_label`（三个端点都带上），`core.js` 只留配色。**刻意保留两套措辞**——居民端「待接收」面向患者、业务端「待接诊」面向医师，同一个状态、两个读者、两套词是对的；不对的是同一套词在前后端各存一份。回归 `test_referral_status_label.py` 四条，含「两套映射覆盖的状态码必须一致」与「后端映射覆盖全部可达状态」。
- ◐ 三套并行子域的其余概念——**逐个核过是不是真孤岛**，不机械造接口：
  - ✅ **患者入组**：确是居民端可感知的孤岛（`/me/archive` 的 `chronic_care` 读 `chronic_patients`，`/spd/archive` 的 `profiles` 读 `spd_enrollments`，看到哪份取决于点了哪个入口）。已加 `GET /api/portal/me/enrollments/all`，做法与转诊完全一致：注册制登记源、服务端 `source` 收窄、老接口字节不动。**刻意不统一分级词汇**：平台 `level` 是 1/2/3（控制良好/需干预/高危）、spd 是 low/mid/high/very_high（并发症风险分层），两把尺子量的不是同一件事，硬映射就是编一个不存在的等价关系——故各留原始码 + 各自中文标签。特征化网各钉一个老接口，九条用例，两处变异（只用平台源 / 硬映射分级）各自转红。
  - ☐ **随访**：平台侧**没有**面向居民的随访列表（`/me/archive` 只给 `next_followup_due` 一个日期），只有 spd 有 `/spd/followups`。所以这不是"两份打架"，而是平台侧缺一块——聚合等于给居民**新增**平台慢病随访视图，属加功能而非消孤岛，需先定要不要给。
  - ☐ **病种目录**：`chronic_disease_types` / `disease_programs` / `spd_programs` 三张表都是**运营配置**，只在管理端出现，居民端根本看不到，不存在"居民看到两份"的问题。真正的重复在配置口径（同一个 `hypertension` 码三处各带各的阈值），那是 ADR-0003 方案 C 的范围，不是读侧聚合能解的。
- ✅ `docs/API_MAP.md` 已更新：写侧仍两套（方案 C 待立项），读侧已聚合。

## Later（C 类重构：**四项均已出 ADR，待批准后逐块动手**）

> 这四项都是对**可运行模块**的结构性改动，按 CLAUDE.md §1.2/§9 必须先有 ADR。
> ADR 已写齐（0006-0009，七段式，含实测数据与分批建议），等的是批准，不是分析。

- ✅ **拆倾倒场收官（ADR-0006 Done）**：`gapfill.py`(1125 行/34 端点/6 前缀) 与 `service_extras.py`(521 行/20 端点) 两个倾倒场**均已删除**，54 个端点全部回到业务前缀。分批做的，每批前先跑「端点 URL 集合零漂移」守卫拿基线。
  - ✅ 前置守卫已就位：`test_refactor_drift_guards.py` 快照 885 个端点，搬漏/改名立刻红；另有 `test_遍历本身没瞎` 防守卫自身失效（写这个守卫时先踩过一次：遍历写错、快照只存下 1 个端点、用例照样绿）。
  - ✅ **第一批 `/api/performance` 已搬回**：5 个 `improvements*` 端点从 `gapfill.py` 移进 `routers/performance.py`（gapfill 1125 → 945 行）。零漂移守卫实测 885 个端点纹丝不动。
  - ⚠️ **搬出来一个真问题**：同一个 `/api/performance` 前缀上挂着**两套鉴权**——原 `performance.py` 的 router 是 `require_roles("director")`，gapfill 那个是 `get_current_user`（登录即可）。这正是 ADR-0006 problem 点名的「鉴权分裂」。搬家**刻意没有合并两个路由**：并成一个会把这 5 个端点从「登录可见」收紧到「仅 director」，那是行为变更不是搬家。收益是此前这个分裂散在两个文件里根本看不见，现在并排躺在同一文件里。已逐端点实测鉴权与搬前一致。
  - ✅ **鉴权口径已定**：逐端点核过之后**不做统一收紧**——5 个里 4 个本就妥当（POST 三个各有 `require_roles` + `assert_org_writable`；`GET /improvements` 有 `scope_org_list` 只给本机构明细）。真正漏的只有 **`GET /improvement-stats`**：它连 `user` 参数都没有，任何登录账号拿到的都是**全县**汇总。修法是加 `scope_org_list(..., stats=True)`（统计走医共体范围、明细走本机构，这个区分 `visibility` 早就建好了），而不是锁成 director-only——`pages-public.js` 把它和明细列表放在同一个 `Promise.all` 里取，锁角色会让还能看列表的人整页报错。全域角色响应与整改前一致。回归四条，含「汇总与同屏列表口径一致」。
  - ✅ **剩余五个前缀一次搬完，`gapfill.py` 已删除**——倾倒场从 1125 行归零。`tcm`/`cssd`/`education`/`maternal` 并入同名模块（四者鉴权与目标 router **完全一致**，可直接合成一个 router，不像 performance 那样存在鉴权分裂）；`homevisits` 新建模块。零漂移守卫实测 885 端点不变。
  - 搬家顺带撞出一处**同名遮蔽**：`maternal.py` 里出现两个 `ScreeningCreate`（儿童筛查 vs 产前筛查）与两个 `list_screenings`。当前行为是对的（各自的使用点都在自己的定义之后、重定义之前），但一个写在 417 行之后、想用儿童筛查 schema 的新端点会**静默拿到产前筛查的校验规则**。已把搬来的那套改名 `PrenatalScreeningCreate`/`list_prenatal_screenings`，并实测两套 422 校验各自正确。
  - `cssd` 与 `performance` 暂时移出 `FULLY_GOVERNED`：搬进来的端点本就无契约，总欠账仍 740（只是换了名下），补完即加回。
  - ✅ **`service_extras.py` 一次拆完并删除（2026-08-24）**：20 个端点 → `exams`(6，报告模板/报告修订/检查资源要素档案) · `cssd`(3，物品申领) · `appointments`(3，服务黑名单) · `consultations`(2，会诊专家) · `education`(2，宣教文章)；`surveys`(3) 与 `triage`(1) **新建模块**——满意度评价的对象横跨签约/就诊/会诊三类，导诊是到院前环节，挂进任何一个既有域都会让别的看起来像附属品，理由写进了各自 docstring。零漂移守卫实测 885 个端点纹丝不动。
  - 搬之前逐项核过三件事，**都清**，所以这批比第一批 `/api/performance` 简单得多：六个目标模块的 router 鉴权与倾倒场**完全一致**（都是 `dependencies=[Depends(get_current_user)]`，不存在鉴权分裂）；**无同名遮蔽**（gapfill 那次撞到过两个 `ScreeningCreate`）；**无路径冲突**。
  - 差点漏一个：`_CENTERS` 常量定义在倾倒场 router 那行的**下面**、不在任何分节里，按分节切块时没带上——ruff 的 F821 当场拦下。分节切块这个手法对"节外的模块级常量"是有盲区的。
- ◐ 统计簇 `analytics/metrics/reports/performance` 合并口径 → **[ADR-0007](docs/adr/0007-统计簇口径合并.md)（Accepted）**。**第一步已交付**：`docs/统计口径对照表.md` —— 逐条比对四个模块的实际代码，摸出 5 处口径分歧 + 1 处更严重的问题：
  - 🔴 **有两个平行的机构评分体系**：`performance/orgs`（`performance_indicators` 表 + 硬编码五维度）与 `analytics/performance-report`（`performance_formulas` 表 + 可编辑公式）。两个接口都叫「机构绩效」、两个分数不可比。
  - ⚠️ **随访覆盖率不带时间窗**：「有过任意一次随访」就永久计入覆盖，数字只涨不跌、随管理年限趋近 100%，基本没有考核价值。更像缺陷而非分歧，但修它会让所有机构该项得分明显下降。
  - 另 4 处分子/分母口径：转诊结案率的分母（`from_org_id` vs 全部）、互认算不算远程诊断服务量、`auto_passed` 算不算处方合格、诊疗人次是否只算基层。
  - ✅ **第 0 条已裁定**：两套评分引擎**都留、分工写死**——`performance/orgs` 做**考核基准**（口径固定才谈得上全县横向可比；且开箱即用、前端三处在用、有契约与特征化网），`analytics/performance-report` 做**自定义分析**（公式可编辑正是探索场景该有的样子，但空表开箱 0 分，做不了基准）。✅ 已落地：两个页面互相点名对方并各自标明口径（考核页还显示评分周期），顺带修掉「运营月报CSV（累计）」这个只对一半的按钮标签；`tests/test_performance_caliber_labels.py` 盯住，含一条"文案别被挪进注释"的防呆用例。
  - ✅ **第 3 条已裁定并落地**：`performance/orgs` 改**周期口径**，`period` 支持 `YYYY`（缺省当年）与 `YYYY-MM`。慢病一项的时间语义**不对称**：分母是在管存量（不按期，去年入组今年仍在管的照样考核），分子是本期随访到的人。响应新增 `period` 字段，前端两处都标出评分周期。回归六条，两处变异验证。
  - ✅ 第 1、2、4 条已裁定（2026-08-22），**三条都不动分数**（分数经 `fund.distribute` 切基金池，无卫健口径文件不动真金白银）：
    - **第 1 条**转诊结案率维持按转出机构。裁定前先修了更要紧的：`PATCH /api/referrals/{id}/status` 当时只有角色守卫、**没有机构归属校验**，任何机构的医师都能把别人的单子接诊/结案（实测 200）——而 `completed` 正是本指标的分子。已补"仅接收方机构可推进"（全域角色放行），归属校验排在状态机之前以免泄露单据状态。`tests/test_referral_status_authority.py` 6 条，三处变异验证。
    - **第 2 条**互认照计。关键是这一项按 `from_org_id`（**申请方/基层**）计，衡量的是平台使用而非诊断工作量，互认正是想要的结果。**随之暴露一条新待裁定**：真正出报告的共享诊断中心在该维度拿 0 分，`claimed_org_id` 已让"按诊断方计分"可行，但属新增计分口径、会改基金分配，需卫健口径支持。已用例断言住"中心拿 0 分"这个事实，改动时会被看见。
    - **第 4 条** `auto_passed` 维持计入，但补 `detail.rx_pass.rule_covered`（本期至少一味药对得上**生效**规则的处方张数，按处方去重）。`auto_passed` 的真实含义是"没有规则被触发"，而 `drug_rules` 全县共用一张表——规则库越稀疏合格率越接近 100% 且越无区分度。照 `ddd` 未维护的既有先例"明说没维护"，而不是按缺省值硬算。追加字段，不改既有字节。
  - ✅ **已修（2026-08-22）**：共享诊断中心的工作量现已计分——`remote_exam` 维度改为两侧都计（申请方按 `from_org_id` 数 reported+recognized，中心按 `claimed_org_id` 只数 reported）。**这会改分数**（中心侧上升）并影响此后的基金分配；已分配的池子是冻结快照不受影响。已知偏差：`claimed_org_id` 是 2026-08 加的列，更早周期的中心侧数字偏低，跨该时点不可同比。
  - ✅ **已改名**：`远程诊断服务量` → `共享诊断协同量`（新库自动生效）。**存量库只报告不代改**——起初写的 `UPDATE` 被平台的迁移数据安全闸门判为 A 档拦下，拦得对：这是管理端可编辑的现场配置，可能已被写进各县考核文件与报表标题。改成升级时打 WARNING 指名该行、处置 SQL 写在 docstring 里，改不改由现场定；不改不影响计分。⚠️ `reports.py` 的同名监测指标是 2024 版国家指标体系的法定名称、全县一个总数，**不改**——同名不同物。
  - ☑ `performance/orgs` 的 **N+1** 已消除：8 条 `GROUP BY org_id` 替代「每机构 8 条 count」，SQL 条数与机构数解耦。18 组参数逐字节比对老实现同值；`test_查询条数不随机构数增长` 用「两次不同机构数的差值」防复发（写死上限会被无关改动误伤）。顺带补上 `test_慢病随访按人去重` —— 原用例每人恰好一次随访，`distinct` 丢了也测不出来。
  - ✅ 周期解析器合并完毕，而且**是三份不是两份**——`cost._period_bounds` 与 `analytics._period_bounds` 逐字节相同（16 组输入实测同值）。合成 `deps.month_bounds` 一份，五个既有端点行为零漂移（含原实现宽松的 `2026-1` 也照收、422 文案一字不改）。
  - ✅ 顺带修掉三份副本共有的一个 **500**：右端点的计算写在 `try` 外面，`9999-12` 的次月是 10000 年，溢出没人接 → `?period=9999-12` 在 5 个端点上返回 500 而不是 422。**`deps.period_bounds` 自己也中招**（本轮周期口径改动里只圈进了年度形式 `9999`，月度形式漏在外面），一并修。`tests/test_period_parsing.py` 27 条守住，三处变异验证。
  - ✅ **已裁定（2026-08-22）：月度解析器不接受 `YYYY`，两个解析器分工保持**。合并会把 5 个既有端点当前的 422 变成 200——那不是去重，是给未受影响的接口加功能；而且「年度成本」「年度运行效率」这些口径本身没定义过（`efficiency` 的日均担负按天数摊，年度窗口下含义完全不同）。想要年度粒度应当是一次带口径设计的新需求，不是解析器合并的副产物。已有 `test_两个解析器分工不同_月度那个不认年度` 防「顺手统一」。
- ✅ **God 文件分域拆包已完成（ADR-0008 Done）**：`spd/routers/config.py`(1549 行) 与 `models.py`(3989 行/187 类) 都已拆成包，**调用方 import 路径一行没改**。下面的子条目记着两次拆包的实测数据与踩到的坑。（顶行此前一直挂着 ◐，是标记没跟上——子条目早就写着 ✅。）
  - ✅ 前置守卫已就位：同一份用例快照 246 个 ORM 类名——拆包漏了重导出会让类不再注册进 `Base.metadata`，**建表静默少一张**而不是报错，这条把「静默」变成「报错」。
  - ✅ **演练完成：`spd/routers/config.py` 1549 行 → 包（8 个文件，最大 331 行）**。按业务分节拆成 catalog / paths / scales / teams / devices / centers 六个子模块 + `_base`（路由对象与跨节工具）。**导入路径一行没改**（`from .routers import config` 照旧），零漂移守卫实测 885 端点 + 246 模型纹丝不动。
  - 拆之前先用 AST 扫了一遍**跨节引用**：`_bump_version`（专病档案↔标准路径）、`_qr_svg`（评估量表↔村医档案）、`_target_out`（管理目标↔专病档案）三个共用件——靠肉眼读 1549 行是找不全的，找漏一个就是 NameError。前两个跨了分组边界，收进 `_base`。
  - 另加两条守卫：**导入路径不变**、**子模块注册顺序与原分节一致**。后者不是洁癖——路由靠 import 时装饰器注册，顺序决定 FastAPI 匹配优先级，乱序**不报错**只会让某些路径悄悄匹配到别的处理函数。变异验证：打乱顺序 → 转红；漏掉一个子模块 → 端点守卫报「消失 4 个」。
  - ✅ **`models.py` 3989 行 / 187 类 → 分域包（16 个文件，最大 505 行）**：core / clinical / emergency / pharmacy / inpatient / chronic / publichealth / contracts / finance / assets / hr / quality / platform / portal + `_base`（Money、utcnow）。**导入路径一行没改**（`from ..models import X` 照旧），零漂移守卫实测 885 端点 + 246 模型不变，空库迁移仍建出 247 张表，`SCHEMA.md` 无 diff。
  - 拆之前先用 AST 查了两件事：① 187 个类是**完整划分**（无遗漏无重复，脚本断言）；② 14 处 `Mapped[SomeClass]` 的**硬顺序依赖**——本仓库没开 `from __future__ import annotations`，这类注解在建类时就要求被引用的类已定义。14 对全部落在同一域内，故各域内保持原文件顺序即可满足。
  - 顺带收紧了 spd 边界用例：`PLATFORM_TOUCHPOINTS` 从**按文件名**匹配改成**按相对路径**。拆包后触点落在 `models/__init__.py`，按文件名放行 `__init__.py` 等于给每个包都开口子。
  - 实测确认："漏掉一个域"**不会静默**——187 个类全都被某处按名 import，漏一个立刻 ImportError。快照守卫仍有价值（挡的是将来新增却没人按名引用的类），已用加一个探针类验证它能报出单个类的增减。
- ◐ 前端组件抽取与工具函数合并（[ADR-0009](docs/adr/0009-前端组件抽取与工具函数合并.md) **Accepted**）。**第一步已完成**：`$` 与 `esc` 三份逐字相同的实现合并进 `static/shared.js`，三个 HTML 入口都把它排在第一个 script；守卫 `test_frontend_shared_utils.py` 十三条（含转义表逐字符、加载顺序、消费方不得再自定义），三处变异各自转红。动机不是整洁而是安全：`esc()` 是 §8 红线，近百处手写插值，一份实现才有一处审查点。**`api()` 刻意未合并**——三套认证语义不同（localStorage / sessionStorage / 不带令牌），连 401 的处理时机与文案都不一样，合并需要把令牌来源与 401 回调参数化，那是行为重构不是去重，留作后续单独一步。
- ◐ ADR-0009 第二步已启动：`panel()` 已抽出（放 core.js 与 `table()` 并列，**不放 shared.js**——`.panel` 是管理端的标记约定，居民端/医师端另有一套）；第一页 `renderServiceRequests` 迁完，Node 比对确认 `panel()` 本身是 no-op。**人工过这一页找出了全量扫描的盲区**：一个解构出来的局部变量（`UNIFIED_STATUS` 查不到时 `text` 回落成后端原始状态码）一直裸插进 innerHTML，扫描只认 `x.y` 属性访问所以漏了。已修并回归。后续逐页迁，不设期限。
- ◐ **ADR-0009 第二批（2026-08-26）：又迁六页**——随访 / 定时任务 / 满意度 / 站内消息 / 质量指标 / 运行监控，`pages-mgmt.js` 里 16 处手写外壳换成 `panel()`，六页输出逐字符一致。三件事值得记：
  - 🔴 **顺着"人工过一页"把上一轮那个盲区按形状扫了一遍，又挖出 33 处同样的未转义**（core 6 / pages-clinical 9 / pages-mgmt 5 / pages-public 11 / m 2）。全是 `const [text, color] = MAP[x.status] || [x.status, ""]` 之后 `${text}` 裸插——映射查不到时 `text` 就是**后端原始状态码**。上一轮撞见这个形状时只修了眼前那一处，**个案修复漏掉了 97%**。已全部修掉并加形状守卫（`test_frontend_escape_guard.py` 新增一条，含防空转与"兜底是字面量不得误报"）；实测该守卫在修复前的代码上正报 33 处、修复后 0 处。教训：撞见一处未转义要立刻扫**形状**，别只修个案。
  - **取证工具做成了可复用的**：`scripts/render_diff.js` + `scripts/fixtures/render_fixtures.json`——在 Node 里按夹具真渲染页面，拿迁移前后的 innerHTML 逐字符比。上一轮那个是一次性脚本；剩下还有 ~300 处外壳要迁，每页重写一遍取证脚本不合算。夹具刻意**塞 XSS 载荷**，这样比对的不只是"标签没挪位"而是"转义行为没变"；三处变异（吃掉 esc / 外壳掉 class / 标题重复转义）各自转红。
  - **标题里的 `esc()` 必须去掉**：`panel()` 自己转义 title，留着就是转两遍（`&` → `&amp;amp;`），是改字节。`renderMonitor` 上实测到 `本实例进程内&lt;b&gt;` → `本实例进程内&amp;lt;b&amp;gt;`，已加守卫。
  - 剩余手写外壳约 300 处（core 43 / pages-clinical 106 / pages-mgmt 56 / pages-spd 52 / pages-public 40），继续逐页迁，不设期限。
  - 🔁 **自审补课（同轮 `/code-review`）**：上面三条新守卫**判据都比缺陷窄**，合成用例逐条试穿全部绕过——限定了"映射名首字母大写"（可 `spdTag(map, key)` 就是小写形参）、"兜底第二元恰好是 `""`"（带默认配色就绕过）、"`panel(` 后紧跟反引号"（标题是单个动态值时最自然的写法没有反引号）。这些差异与缺陷无关，纯是拼写；判据窄于缺陷 = 留了个**看不见的**后门（守卫还是绿的）。已放宽到同宽并把绕过写法钉成参数化回归；放宽前后在修复前代码上都正报 33 处（没有靠放宽把账做小）。**取证工具自己也被验了一遍**，查出五处"看着绿其实没验到"，最要紧的是空表假绿——夹具"列表必须非空"只写在注释里没真检，而空列表下 `table()` 照样吐几百字符的"暂无数据"表格，于是报"逐字符相同"而行模板一次都没求值。已改为显式检出并报红。
  - 登记一条不在本轮修的（`docs/TECH_DEBT.md`）：**P2-25** 裸 `${MAP[key]}` 查不到时显示字面量 `undefined`（同族，但只是显示缺陷、非崩溃非 XSS，刻意不并进那两条守卫——一条守卫混两种严重度迟早因噪声被加豁免；且需逐处定文案）。
- ✅ **P2-26 收敛：34 处状态标签收进 `statusTag()` 组件**（同日续做）。那 33 处未转义**全是在手抄同一段三行代码**，而同一个仓库里 `spdTag()` 早就写对了——只是没铺满。这正是 ADR-0009 的论点（"转义收进组件就漏不掉"）第二个、也是有血的样本。三处判断：
  - **放 `shared.js` 而不是 `core.js`**（`panel()` 放的是 core.js）。判据是"三端是不是真的都在用"：`.panel`/`.card` 是管理端独有的，而 `.tag` 三套前端都在用且标记契约逐字相同（`style.css:60` 与 `m/m.css:141` 各自定义 `.tag` 与 `.tag.red/.green/.orange`，配色不同、类名约定一致）。
  - **合并实现 ≠ 统一行为**：慢专病历来把空状态显示成 `—`、管理端显示空白，两套都保住（`spdTag` 委托时自己传 `key || "—"`）。顺手统一那是改字节，不是去重。
  - **等价性是证出来的不是推出来的**：`scripts/statustag_equiv.js` 把两种写法在输入矩阵上逐字符比（命中/未命中/空串/null/undefined/XSS 载荷/**数字状态码**）。数字那条当场抓到真问题——初版组件用 `key || ""` 兜底，会把慢病分级的 `0` 吞成空白，改用 `key ?? ""`。慢专病侧的委托还有个**前提**（映射表不能用假值当键），脚本里真的扫了 5 张表来验，没有默认它成立。
  - 取证：10 个页面渲染字节比对一致（含两处**手工**改的 `PAY_STATUS`/`ESB_MSG_STATUS` —— 脚本因为把 CSS 的 `color:` 误判成"第二处用途"而跳过了它们，跳过是对的，机械替换那两处会出错）。比对器顺带补上一个盲区：原先只捕 `#page-body`，`renderEsb` 这类由内层函数写进子容器的表格根本没进比对，现改为捕获全部容器。⚠️ 居民端/医生端的 4 处只有等价性证明与人工复核，**没有字节比对**——比对器只加载管理端那套文件。
- ✅ **顺手修（同一次形状扫描带出来的第三种失败方式）**：`pages-clinical.js` 的缺药登记 `const [t, col] = SS[s.status]` **没有兜底**，而后端会把状态写成 `collected`/`no_show`/`cancelled`（`medication.py` 的 `shortage.status = body.result`）——`SS` 里没有这三个，解构 undefined 抛 TypeError，且这一句在 `table()` 的行渲染回调里，所以炸的是**整页**：只要有一条缺药登记结了案，这一页就再也打不开。已用比对器复现（旧版抛 `undefined is not iterable`，新版正常）并按仓库既有写法修掉（`|| [s.status, ""]` + `esc`）。全仓库扫过只此一处，已加守卫。

## 待决策（先 ADR，后动手）

- ✅ ADR-0002 生产迁移停用 create_all（Accepted，已实施）。
- ✅ ADR-0003 三套并行子域收敛（**Accepted**：先 B 读侧聚合、中期再评估 C、否决 D）。转诊那步已落地（接口+居民端），其余概念待续。
- ✅ ADR-0006 倾倒场路由回归业务前缀（**Accepted**）。第一步「端点 URL 集合零漂移」守卫已就位。
- ✅ ADR-0007 统计簇口径合并（**Accepted**）。⚠️ 但第一步仍**不是写代码**——需先出「同名指标在几处各算什么」的对照表交产品裁定。
- ✅ ADR-0008 God 文件分域拆包（**Accepted**）。第一步「模型名字集合零漂移」守卫已就位。
- ✅ ADR-0009 前端组件抽取与工具函数合并（**Accepted**）。第一步（合并 `$`/`esc`）已完成。

---

## Done（已完成，倒序）

- ✅ 童子军法则写入 CLAUDE.md §12。
- ✅ ADR 体系建立（`docs/adr/`：README + 模板 + 0001/0002/0003）。
- ✅ 核心数据不可变定义 + 守卫（`test_core_data_invariants.py`）。
- ✅ 数据模型治理：SCHEMA 输出、核心表冻结、created_at 棘轮。
- ✅ 接口标准治理棘轮 + 样板迁移（checkups）。
- ✅ 特征化测试指引 + spd/rules 样板网。
- ✅ 模块分级 A/B/C/D。
- ✅ 六项能力（build/lint/typecheck/unit/integration/smoke）+ Makefile + CI 接入。
- ✅ AS-IS 架构审计 + 六张地图。
