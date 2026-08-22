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
- ✅ 迁移-模型**列级** parity 门（`test_迁移与模型的列集合零漂移`）：真 PG 上跑完 `upgrade heads` 后逐表比对 inspector 与 `Base.metadata` 的列集合，两个方向都查（模型有迁移没建 → 生产 UndefinedColumn；迁移建了模型没有 → 多半是删列忘写迁移）。ADR-0002 停用 `create_all` 后这是最后一处「开发 SQLite 看着正常、上线才炸」的口子。已变异验证：给模型加一列不写迁移，当场转红。
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

- ◐ 接口契约棘轮：按 `docs/接口标准与治理.md` 逐块迁移。已治理 12 模块，基线 **757→741**。本轮做掉 encounters 的 `/archive/360`——全平台聚合度最高的接口（一次吐出一个人的就诊/检查/慢病/处方/结算/体检），嵌套九段**逐段建模**而不是 `dict[str, Any]`（写成 Any 等于没声明契约，而这个接口恰恰最需要）。先补特征化网 `test_archive_360_contract.py` 十三条钉住键集合与类型、再加契约、加完网照样绿（响应字节不变）；网里六段都造了数据——空列表什么都钉不住。两处变异（契约少一个字段 / 字段名写错）各自转红。本轮再做掉 performance 的 `/orgs` 机构计分卡，基线 **741 → 740**，`performance` 三个端点全部有契约、已进 `FULLY_GOVERNED`。两处麻烦点：`weights` 的键来自指标表是**动态的**（只能 `dict[str, float]`）；`detail` 五段**混形状**（三段分子/分母、两段裸计数），逐段建模而非 `dict[str, Any]`。`score` 特意验过恒为 float——`_normalized_weights` 表空时退回非空默认，求和恒在浮点上做；若可能是 int，声明 float 就会把 `0` 变 `0.0`，那是改字节不是治理。特征化网 `test_performance_orgs_contract.py` 十条（造了五个维度的真实数据，全零响应什么都钉不住），并逐字钉住折算值（42.5 / 32.5 / 67.5）；三组参数下**响应字节逐字节比对一致**；两处变异各自转红。下一批候选：`analytics` / `metrics` 两簇（注意与 ADR-0007 口径合并有交集，先做契约不动口径）。
- ✅ **搬家带来的 8 个端点契约已补齐**，基线 **740 → 732**，`cssd` 与 `performance` 双双回归 `FULLY_GOVERNED`。最容易踩的是 `cssd` 的 `total_cost`：它是 `round(totals.get(id, 0), 2)`，**没有成本项时返回 int `0` 而不是 `0.0`**，声明成 `float` 就会把 `0` 变成 `0.0`（改字节）。契约写 `int | float` 原样透传，变异验证：改成 `float` 立刻转红。八个端点响应字节逐字节比对一致（含空集与有数据两条分支）。
- ✅ `created_at` 欠账迁移**收官**：52 → **2**（16 个迁移批次，平台链+spd 链，全部常量默认回填→batch 撤默认范式，全程响应字节不变）。仅剩有意留置 2 张：`blood_stocks`（小型 upsert 表，价值低降级）、`admissions`（核心表，改列需先 ADR）。此后新表一律带 created_at（棘轮基线=2 顶住）。
- ✅ 测试隔离修复：**七条**用例在 `pytest -k` 子集下会红（整模块跑得过，纯靠执行顺序）。根因不是共享库本身，是**跨用例借数据**——`GET …?keyword=张三` 取上一条用例建的人、`enrollments[0]` 取上一条用例建的档案、DRG 统计里指望上一条用例的病例已在。改法：把前置数据交给**幂等的模块级 fixture** 负责交付（已有就返回、没有才建），用例声明依赖即可。`path_template` fixture 顺带把模板发布掉——「可用的模板」本就该由建它的 fixture 交付，而不是靠 `test_publish_*` 排在前面顺带做掉。现每条单选跑均绿。
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

- ◐ 拆倾倒场 `gapfill.py`(1125 行/34 端点/6 前缀) / `service_extras.py`(521 行/20 端点) 回业务前缀 → **[ADR-0006](docs/adr/0006-倾倒场路由回归业务前缀.md)（**Accepted**）**。建议按前缀分批，第一批 `/api/performance`（与 `routers/performance.py` 前缀重叠、只有 2 个端点）；动手前先加「端点 URL 集合零漂移」守卫。
  - ✅ 前置守卫已就位：`test_refactor_drift_guards.py` 快照 885 个端点，搬漏/改名立刻红；另有 `test_遍历本身没瞎` 防守卫自身失效（写这个守卫时先踩过一次：遍历写错、快照只存下 1 个端点、用例照样绿）。
  - ✅ **第一批 `/api/performance` 已搬回**：5 个 `improvements*` 端点从 `gapfill.py` 移进 `routers/performance.py`（gapfill 1125 → 945 行）。零漂移守卫实测 885 个端点纹丝不动。
  - ⚠️ **搬出来一个真问题**：同一个 `/api/performance` 前缀上挂着**两套鉴权**——原 `performance.py` 的 router 是 `require_roles("director")`，gapfill 那个是 `get_current_user`（登录即可）。这正是 ADR-0006 problem 点名的「鉴权分裂」。搬家**刻意没有合并两个路由**：并成一个会把这 5 个端点从「登录可见」收紧到「仅 director」，那是行为变更不是搬家。收益是此前这个分裂散在两个文件里根本看不见，现在并排躺在同一文件里。已逐端点实测鉴权与搬前一致。
  - ✅ **鉴权口径已定**：逐端点核过之后**不做统一收紧**——5 个里 4 个本就妥当（POST 三个各有 `require_roles` + `assert_org_writable`；`GET /improvements` 有 `scope_org_list` 只给本机构明细）。真正漏的只有 **`GET /improvement-stats`**：它连 `user` 参数都没有，任何登录账号拿到的都是**全县**汇总。修法是加 `scope_org_list(..., stats=True)`（统计走医共体范围、明细走本机构，这个区分 `visibility` 早就建好了），而不是锁成 director-only——`pages-public.js` 把它和明细列表放在同一个 `Promise.all` 里取，锁角色会让还能看列表的人整页报错。全域角色响应与整改前一致。回归四条，含「汇总与同屏列表口径一致」。
  - ✅ **剩余五个前缀一次搬完，`gapfill.py` 已删除**——倾倒场从 1125 行归零。`tcm`/`cssd`/`education`/`maternal` 并入同名模块（四者鉴权与目标 router **完全一致**，可直接合成一个 router，不像 performance 那样存在鉴权分裂）；`homevisits` 新建模块。零漂移守卫实测 885 端点不变。
  - 搬家顺带撞出一处**同名遮蔽**：`maternal.py` 里出现两个 `ScreeningCreate`（儿童筛查 vs 产前筛查）与两个 `list_screenings`。当前行为是对的（各自的使用点都在自己的定义之后、重定义之前），但一个写在 417 行之后、想用儿童筛查 schema 的新端点会**静默拿到产前筛查的校验规则**。已把搬来的那套改名 `PrenatalScreeningCreate`/`list_prenatal_screenings`，并实测两套 422 校验各自正确。
  - `cssd` 与 `performance` 暂时移出 `FULLY_GOVERNED`：搬进来的端点本就无契约，总欠账仍 740（只是换了名下），补完即加回。
- ◐ 统计簇 `analytics/metrics/reports/performance` 合并口径 → **[ADR-0007](docs/adr/0007-统计簇口径合并.md)（Accepted）**。**第一步已交付**：`docs/统计口径对照表.md` —— 逐条比对四个模块的实际代码，摸出 5 处口径分歧 + 1 处更严重的问题：
  - 🔴 **有两个平行的机构评分体系**：`performance/orgs`（`performance_indicators` 表 + 硬编码五维度）与 `analytics/performance-report`（`performance_formulas` 表 + 可编辑公式）。两个接口都叫「机构绩效」、两个分数不可比。
  - ⚠️ **随访覆盖率不带时间窗**：「有过任意一次随访」就永久计入覆盖，数字只涨不跌、随管理年限趋近 100%，基本没有考核价值。更像缺陷而非分歧，但修它会让所有机构该项得分明显下降。
  - 另 4 处分子/分母口径：转诊结案率的分母（`from_org_id` vs 全部）、互认算不算远程诊断服务量、`auto_passed` 算不算处方合格、诊疗人次是否只算基层。
  - ✅ **第 0 条已裁定**：两套评分引擎**都留、分工写死**——`performance/orgs` 做**考核基准**（口径固定才谈得上全县横向可比；且开箱即用、前端三处在用、有契约与特征化网），`analytics/performance-report` 做**自定义分析**（公式可编辑正是探索场景该有的样子，但空表开箱 0 分，做不了基准）。✅ 已落地：两个页面互相点名对方并各自标明口径（考核页还显示评分周期），顺带修掉「运营月报CSV（累计）」这个只对一半的按钮标签；`tests/test_performance_caliber_labels.py` 盯住，含一条"文案别被挪进注释"的防呆用例。
  - ✅ **第 3 条已裁定并落地**：`performance/orgs` 改**周期口径**，`period` 支持 `YYYY`（缺省当年）与 `YYYY-MM`。慢病一项的时间语义**不对称**：分母是在管存量（不按期，去年入组今年仍在管的照样考核），分子是本期随访到的人。响应新增 `period` 字段，前端两处都标出评分周期。回归六条，两处变异验证。
  - ☐ 第 1、2、4 条待裁定：转诊结案率的分母、互认算不算远程诊断服务量、`auto_passed` 算不算处方合格。
  - ☑ `performance/orgs` 的 **N+1** 已消除：8 条 `GROUP BY org_id` 替代「每机构 8 条 count」，SQL 条数与机构数解耦。18 组参数逐字节比对老实现同值；`test_查询条数不随机构数增长` 用「两次不同机构数的差值」防复发（写死上限会被无关改动误伤）。顺带补上 `test_慢病随访按人去重` —— 原用例每人恰好一次随访，`distinct` 丢了也测不出来。
  - ☐ `deps.period_bounds` 与 `analytics._period_bounds` 两份周期解析器待合并（后者只认 `YYYY-MM`；合并会让它对 `YYYY` 从 422 变 200，属对未受影响接口的行为变更，另案）。
- ◐ God 文件 `models.py`(3989 行/187 类) / `spd/routers/config.py`(1549 行) 分域拆包 → **[ADR-0008](docs/adr/0008-God文件分域拆包.md)（**Accepted**）**。拆成包 + `__init__.py` 重导出，**调用方 import 路径一行不改**；先拆 spd config 演练再动 models；动手前先加「模型名字集合零漂移」守卫。
  - ✅ 前置守卫已就位：同一份用例快照 246 个 ORM 类名——拆包漏了重导出会让类不再注册进 `Base.metadata`，**建表静默少一张**而不是报错，这条把「静默」变成「报错」。
  - ✅ **演练完成：`spd/routers/config.py` 1549 行 → 包（8 个文件，最大 331 行）**。按业务分节拆成 catalog / paths / scales / teams / devices / centers 六个子模块 + `_base`（路由对象与跨节工具）。**导入路径一行没改**（`from .routers import config` 照旧），零漂移守卫实测 885 端点 + 246 模型纹丝不动。
  - 拆之前先用 AST 扫了一遍**跨节引用**：`_bump_version`（专病档案↔标准路径）、`_qr_svg`（评估量表↔村医档案）、`_target_out`（管理目标↔专病档案）三个共用件——靠肉眼读 1549 行是找不全的，找漏一个就是 NameError。前两个跨了分组边界，收进 `_base`。
  - 另加两条守卫：**导入路径不变**、**子模块注册顺序与原分节一致**。后者不是洁癖——路由靠 import 时装饰器注册，顺序决定 FastAPI 匹配优先级，乱序**不报错**只会让某些路径悄悄匹配到别的处理函数。变异验证：打乱顺序 → 转红；漏掉一个子模块 → 端点守卫报「消失 4 个」。
  - ✅ **`models.py` 3989 行 / 187 类 → 分域包（16 个文件，最大 505 行）**：core / clinical / emergency / pharmacy / inpatient / chronic / publichealth / contracts / finance / assets / hr / quality / platform / portal + `_base`（Money、utcnow）。**导入路径一行没改**（`from ..models import X` 照旧），零漂移守卫实测 885 端点 + 246 模型不变，空库迁移仍建出 247 张表，`SCHEMA.md` 无 diff。
  - 拆之前先用 AST 查了两件事：① 187 个类是**完整划分**（无遗漏无重复，脚本断言）；② 14 处 `Mapped[SomeClass]` 的**硬顺序依赖**——本仓库没开 `from __future__ import annotations`，这类注解在建类时就要求被引用的类已定义。14 对全部落在同一域内，故各域内保持原文件顺序即可满足。
  - 顺带收紧了 spd 边界用例：`PLATFORM_TOUCHPOINTS` 从**按文件名**匹配改成**按相对路径**。拆包后触点落在 `models/__init__.py`，按文件名放行 `__init__.py` 等于给每个包都开口子。
  - 实测确认："漏掉一个域"**不会静默**——187 个类全都被某处按名 import，漏一个立刻 ImportError。快照守卫仍有价值（挡的是将来新增却没人按名引用的类），已用加一个探针类验证它能报出单个类的增减。
- ◐ 前端组件抽取与工具函数合并（[ADR-0009](docs/adr/0009-前端组件抽取与工具函数合并.md) **Accepted**）。**第一步已完成**：`$` 与 `esc` 三份逐字相同的实现合并进 `static/shared.js`，三个 HTML 入口都把它排在第一个 script；守卫 `test_frontend_shared_utils.py` 十三条（含转义表逐字符、加载顺序、消费方不得再自定义），三处变异各自转红。动机不是整洁而是安全：`esc()` 是 §8 红线，近百处手写插值，一份实现才有一处审查点。**`api()` 刻意未合并**——三套认证语义不同（localStorage / sessionStorage / 不带令牌），连 401 的处理时机与文案都不一样，合并需要把令牌来源与 401 回调参数化，那是行为重构不是去重，留作后续单独一步。
- ☐ ADR-0009 第二步：抽 `panel()`/`table()` 并**逐页**迁移，每次只迁一页，不设完成期限。

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
