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

### 一行/小修（童子军级，碰到即修）
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
- 🔧 **取证方式换了**，这是本轮真正的产出：不再每个模块手写一份捕获脚本，改成给 app 装一个中间件，把**整个测试套件**跑出来的每个响应按 `(方法, 路由模板, 状态码)` 记下字节，加契约前后各跑一次逐项比对——一次覆盖 **1847 个组合**，治理十个模块和治理一个模块的取证成本一样。工具 `tests/capture_plugin.py`，用法与**四条注意事项**（第四条见下方 08-24 那条）写进了 `docs/接口标准与治理.md`：①用之前必须先量噪声底（同一份代码跑两次差异必须为 0，实测第一次有 2 处随机项）；②它覆盖不到测试没跑过的端点，那是**沉默的缺口**不是保证；③改了路由代码就要重跑（pytest 启动时导入 app，之后的编辑不影响正在跑的那轮——踩过一次）。
- 本轮的建模判断：Money 列一律 `int | float`（accounting 全模块、fund 全模块、materials 采购与耗材单价——fund 那个直接决定各机构分到多少钱，不是显示问题）；条件键用 `exclude_unset`（accounting 凭证的 entries、rbac 内置角色的 note、fund 超额预付的 warning、**tcm_heritage 决策点的 answer/explain**——最后这个是嵌套条件键，学员拉题目时答案整个键不出现，声明成可选字段等于把答案的存在公告出去）；「新建回执」与「列表行」键集合不同的一律两个模型，不硬套继承。
- ✅ **ADR-0006 收官批的 20 个端点已补契约，基线 470 → 450**（覆盖率 52.38%）。`cssd` 加回 `FULLY_GOVERNED`（搬家那个提交里因搬入 3 个无契约端点短暂移出——**总欠账当时一点没变**，470→470 只是换了名下，只有总基线的话那次回退会完全静默）；`surveys`/`triage` 生而全契约。两处会改字节的判断：`ExamResource.price` 是 Money 列（捕获里实测到 `"price":240`，声明 float 就变 `240.0`）；`survey_stats` 的**字段顺序**照 handler 实际出键排——它 `pop("count")` 后又重新赋值，`count` 因此被挪到 `distribution` 与 `negative` 之后。套件级捕获 1856 个组合前后比对，**落在这 20 个端点内的差异 0 处**；捕获盖不到的两处另补了用例（`GET /api/surveys` 一次没被跑过、`GET /api/cssd/requests` 只跑到过空列表——空集钉不住字段），四处变异各自转红。
- ✅ **spd 三个业务域做完：`assess`(23) → `care`(31) → `followup`(29)，基线 450 → 427 → 396 → 367**（覆盖率 61.2%，零欠账模块 40 个），三个模块各一个提交、各一份用例文件。三处值得记的判断：①`assess` 的 `GET /scores-analysis` **刻意不加契约**——它两个分支的键**集合与顺序都不同**（空数据 4 键、有数据 5 键且 `average` 位置不同），实测单个 Pydantic 模型只能满足一个分支；用宽字典是逃避、改 handler 是行为变更不该夹在契约批里，故留在欠账并写明原因，`test_scores_analysis_两分支形状不一致` 把这个事实钉成一条会红的用例——哪天有人统一了两分支，它会红，提醒把契约补上。底下那个真问题（空数据时 `ranking` 键整个消失，前端 `data.ranking.map()` 会 TypeError）另行登记，目前无前端消费方。②`care` 的 `by_item` 是**两级动态字典**（`dict[str, dict[str, int]]`），`RevisitOut.items` 是 `String(512)` 存的逗号串**不是数组**。③`followup` 的 `points` 让我栽了一次：JSON 列里存的是随访天偏移 `[1,7,30,90]`，我按「JSON 列多半存对象」声明成 `list[dict]`，加上契约当场 **500**，套件连带红 8 条——这不是字节漂移，是接口不可用。判据只能是**写入方**：同一份文件的 `FollowupRuleIn.points` 早写着 `list[int]`、校验是 `p < 0 or p > 3650`，我一处都没看。已写进治理文档的**陷阱三：JSON 列没有元素类型**（另一次是 `assess` 的 `weight`，int `100` 声明成 float 就变 `100.0`）。三批合计 21 处变异各自转红（每处只红一条用例，无冗余无空转）。
- 🔧 **重量噪声地板，发现它长回来了，并修好了工具**。文档原先写「同一份代码跑两次实测 2 处随机项，归一化后归零」——2026-08-24 重量是 **1879 个组合里 76 个有差异**。三类噪声漏在归一化表外：随机 `EHC…` 卡号、**空格分隔**的墙钟串（原 `<TS>` 只认 ISO 的 `T` 形式）、随机实例 id。补进 `capture_plugin.py` 后全局 **76 → 26**，`spd` 七个模块**全部归零**。两条由此确立的规矩：①**比对必须按模块划范围**——那 76 个全局差异没有一个落在被治理的模块内，只看全局会得出「改坏了」的错觉；②**噪声底要按模块量、每批重量**，不能引用一次旧结论。实测值已列进 `docs/接口标准与治理.md`：`spd/followup` 47 组合噪声 **0**（所以本批「38 组合 0 差异」是证据不是巧合），而下一批要动的 `spd/population` 补齐前噪声是 **4**——不先压回 0 就动手，「0 差异」和「噪声恰好一样」分不开。平台侧还剩 26 个噪声组合（monitor 累计计数、esb 令牌、id 排序等）已在文档里点名登记，治理到那些模块前必须先归一化。
- 🔧 **捕获注意事项加到第四条**：前后两轮**必须用同一套测试集**。`assess` 那批的 after 轮带上了本批新写的用例文件，它种了数据又中途 `reset_database()`，10 个端点显示「有差异」实际全是数据不同（`"code":"ac_ratio"` vs `"ratio_rule"`、`total:1` vs `total:0`）——这一条与前三条不同，它产生的是**假阳性**：前三条让你少证据，这条让你看见不存在的回归。识别办法是先对 `N passed` 与组合数（2339/2344、1857/1861 就是当时的提示）。复核 after 轮时 `--ignore` 掉本批新增的用例文件即可。
- 剩余候选：`spd/population`(28) / `education`(21) / `quality`(20) / `admin_mgmt`(20) / `spd/tasks`(19) / `billing`(15) / `spd/referral`(14) / `inpatient`(14)…（共 367，模块数 47）。
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
